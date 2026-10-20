"""The Local Team Browser's Docker lifecycle against an in-memory Docker: admission, generations, removal, exec."""

import json
import socket as sockets
import struct
import threading
import time
import unittest
from pathlib import PurePosixPath
from unittest import mock

from docker.errors import APIError, NotFound

from local import labels as local_labels
from local.browser import runtime
from local.validation import space_prefix

SPACE = "space-0123456789abcdef01234567"
IMAGE = "localhost/shimpz-browser@sha256:" + "a" * 64
PUBLISHED = "ghcr.io/theshimpz/shimpz-browser@sha256:" + "b" * 64


def _probe(generation: int, code: int = 0) -> list[dict[str, object]]:
    """The egress healthcheck's recorded probes: the latest names the generation of the process that answered it."""
    return [{"ExitCode": code, "Output": f"{generation:032x}\n"}]


def _generation(restarts: int) -> runtime.Generation:
    return runtime.Generation("egress-id", f"2026-10-17T00:00:{restarts:02d}Z", f"{restarts:032x}")


def _frame(payload: bytes) -> bytes:
    return struct.pack(">BxxxL", 1, len(payload)) + payload


class Raw:
    def __init__(self, sock):
        self._sock = sock


class FakeContainer:
    def __init__(self, docker, identifier, name, attrs):
        self.docker, self.id, self.name, self.attrs = docker, identifier, name, attrs
        self.restarts = 0

    def reload(self):
        if self.docker.fail.get("reload"):
            raise APIError("reload failed")
        if self.id not in self.docker.containers_by_id:
            raise NotFound("gone")
        if self.docker.probe_lag and self is self.docker.egress:
            # The new process's first probe has not run yet: Docker still shows the old process's last one.
            self.docker.probe_lag -= 1
            if not self.docker.probe_lag:
                self.attrs["State"]["Health"]["Log"] = _probe(self.restarts)

    def start(self):
        self.attrs["State"]["Running"] = True
        self.docker.on_start(self)

    def restart(self, timeout):
        assert timeout == 5
        if self.docker.fail.get("restart"):
            raise APIError("restart failed")
        self.restarts += 1
        self.attrs["State"]["StartedAt"] = f"2026-10-17T00:00:{self.restarts:02d}Z"
        self.attrs["State"]["Health"]["Status"] = self.docker.health_after_restart
        if not self.docker.probe_lag:
            self.attrs["State"]["Health"]["Log"] = _probe(self.restarts)


class Containers:
    def __init__(self, docker):
        self.docker = docker

    def list(self, **options):
        assert options["all"] is True
        filters = options["filters"]
        if self.docker.fail.get("list"):
            raise APIError("list failed")
        wanted = dict(item.split("=", 1) for item in filters["label"])
        return [
            container
            for container in self.docker.containers_by_id.values()
            if all(container.attrs["Config"]["Labels"].get(key) == value for key, value in wanted.items())
        ]

    def get(self, key):
        if self.docker.fail.get("get"):
            raise APIError("get failed")
        if self.docker.fail.get("stall-get"):
            self.docker.fail["stall-get"].wait()
        if self.docker.lingering.get(key, 0) > 0:
            # A removal under way: Docker still lists the container for a few checks.
            self.docker.lingering[key] -= 1
            return self.docker.gone[key]
        for container in self.docker.containers_by_id.values():
            if key in (container.id, container.name):
                return container
        raise NotFound(key)

    def create(self, **arguments):
        if self.docker.fail.get("create"):
            raise APIError("create failed")
        self.docker.created.append(arguments)
        attrs = self.docker.created_attrs(arguments)
        container = FakeContainer(self.docker, f"browser-{len(self.docker.created)}", arguments["name"], attrs)
        self.docker.containers_by_id[container.id] = container
        return container


class Network:
    def __init__(self, docker):
        self.docker = docker
        self.attrs = {
            "Internal": True,
            "EnableIPv6": False,
            "Options": {"com.docker.network.bridge.gateway_mode_ipv4": "isolated"},
            "Containers": {},
        }

    def reload(self):
        members = {
            container.id: {}
            for container in self.docker.containers_by_id.values()
            if runtime.NETWORK in container.attrs["NetworkSettings"]["Networks"]
        }
        self.attrs["Containers"] = {**members, **self.docker.extra_members}


class Networks:
    def __init__(self, docker):
        self.docker = docker

    def get(self, name):
        if self.docker.fail.get("network") or name != runtime.NETWORK:
            raise NotFound(name)
        return self.docker.network


class Images:
    def __init__(self, docker):
        self.docker = docker

    def get(self, reference):
        if self.docker.fail.get("image-get"):
            raise APIError("image failed")
        if reference not in self.docker.images:
            raise NotFound(reference)
        return type("Image", (), {"id": self.docker.images[reference]})()

    def pull(self, repository, tag):
        if self.docker.fail.get("pull"):
            raise APIError("pull failed")
        reference = f"{repository}@{tag}"
        self.docker.images[reference] = "sha256:pulled"
        self.docker.pulled.append(reference)
        return self.get(reference)


class Api:
    def __init__(self, docker):
        self.docker = docker
        self.execs = {}

    def remove_container(self, identifier, force):
        assert force is True
        if self.docker.fail.get("remove"):
            raise APIError("remove failed")
        if self.docker.fail.get("survive"):
            return
        if self.docker.fail.get("stall-remove"):
            self.docker.fail["stall-remove"].wait()
        if identifier in self.docker.removing:
            # Another caller's removal of the same container is under way.
            raise APIError("removal in progress", response=type("Response", (), {"status_code": 409})())
        container = self.docker.containers_by_id.pop(identifier, None)
        if container is None:
            raise NotFound(identifier)
        if self.docker.linger:
            self.docker.lingering[identifier] = self.docker.linger
            self.docker.gone[identifier] = container
        self.docker.removed.append(identifier)

    def exec_create(self, identifier, argv, **options):
        assert options == {
            "stdin": True,
            "stdout": True,
            "stderr": True,
            "privileged": False,
            "user": runtime.USER,
            "workdir": str(PurePosixPath("/") / "tmp"),
        }
        if self.docker.fail.get("exec"):
            raise APIError("exec failed")
        exec_id = f"exec-{len(self.execs)}"
        self.execs[exec_id] = (identifier, tuple(argv[1:]))
        return {"Id": exec_id}

    def exec_start(self, exec_id, socket):
        assert socket is True
        _identifier, arguments = self.execs[exec_id]
        if self.docker.fail.get("stall-start"):
            self.docker.fail["stall-start"].wait()
        if self.docker.fail.get("start"):
            raise APIError("start failed")
        if self.docker.fail.get("no-socket"):
            return object()
        team, docker = sockets.socketpair()
        self.docker.sockets.extend((team, docker))
        code, output = self.docker.programs.get(arguments, (0, b""))
        self.execs[exec_id] = (code, arguments)
        received = self.docker.inputs.setdefault(arguments, bytearray())

        def take_input():
            # Everything Team writes to the program's input, until it closes it.
            while chunk := docker.recv(65536):
                received.extend(chunk)

        threading.Thread(target=take_input, daemon=True).start()
        docker.sendall(_frame(output) if output else b"")
        docker.shutdown(sockets.SHUT_WR)
        return Raw(team)

    def exec_inspect(self, exec_id):
        return {"ExitCode": self.execs[exec_id][0]}


class FakeDocker:
    def __init__(self):
        self.fail = {}
        self.created, self.removed, self.pulled, self.sockets = [], [], [], []
        # Removals under way elsewhere, and how many checks a removed container stays listed.
        self.removing, self.linger, self.lingering, self.gone = set(), 0, {}, {}
        self.extra_members = {}
        self.health_after_restart = "healthy"
        self.probe_lag = 0
        self.images = {IMAGE: "sha256:image", PUBLISHED: "sha256:published"}
        self.programs = {("ready",): (0, b"")}
        self.inputs = {}
        self.containers_by_id = {}
        self.containers = Containers(self)
        self.networks = Networks(self)
        self.images_api = Images(self)
        self.api = Api(self)
        self.network = Network(self)
        egress_labels = {
            local_labels.MANAGED_LABEL: "1",
            local_labels.SPACE_LABEL: SPACE,
            local_labels.KIND_LABEL: runtime.EGRESS_KIND,
        }
        self.egress = FakeContainer(
            self,
            "egress-id",
            runtime.EGRESS,
            {
                "Config": {"Labels": egress_labels},
                "State": {
                    "Running": True,
                    "StartedAt": "2026-10-17T00:00:00Z",
                    "Health": {"Status": "healthy", "Log": _probe(0)},
                },
                # The egress also has its outbound network; the Browser reaches it only on the Browser network.
                "NetworkSettings": {
                    "Networks": {
                        runtime.NETWORK: {"IPAddress": "172.30.0.2"},
                        "shimpz-space-browser-egress-out": {"IPAddress": "172.31.0.2"},
                    }
                },
            },
        )
        self.containers_by_id[self.egress.id] = self.egress
        self.drift = {}

    @property
    def images(self):
        return self._images

    @images.setter
    def images(self, value):
        self._images = value

    def created_attrs(self, arguments):
        tmpfs = dict(arguments["tmpfs"])
        host = {
            "ReadonlyRootfs": arguments["read_only"],
            "CapDrop": arguments["cap_drop"],
            "CapAdd": None,
            "Privileged": arguments["privileged"],
            "SecurityOpt": arguments["security_opt"],
            "Init": arguments["init"],
            "IpcMode": arguments["ipc_mode"],
            "CgroupnsMode": arguments["cgroupns"],
            "Memory": arguments["mem_limit"],
            "MemorySwap": arguments["memswap_limit"],
            "NanoCpus": arguments["nano_cpus"],
            "PidsLimit": arguments["pids_limit"],
            "NetworkMode": arguments["network"],
            "Binds": None,
            "Tmpfs": tmpfs,
            **self.drift,
        }
        return {
            "Name": f"/{arguments['name']}",
            "Image": self.images.get(arguments["image"], "sha256:other")
            if not self.fail.get("image-mismatch")
            else "x",
            "Config": {"User": arguments["user"], "Labels": dict(arguments["labels"])},
            "HostConfig": host,
            "Mounts": [],
            "State": {"Running": False},
            "NetworkSettings": {"Networks": {arguments["network"]: {"IPAddress": "172.30.0.9"}}},
        }

    def on_start(self, container):
        pass


class FakeClient:
    """The docker SDK surface the runtime uses."""

    def __init__(self, docker):
        self.containers = docker.containers
        self.networks = docker.networks
        self.images = docker.images_api
        self.api = docker.api


class BrowserRuntimeTests(unittest.TestCase):
    def setUp(self):
        self.docker = FakeDocker()
        self.now = [0.0]
        self.runtime = self.make()
        self.addCleanup(lambda: [sock.close() for sock in self.docker.sockets])

    def make(self, image=IMAGE, architecture="amd64"):
        def sleep(seconds):
            self.now[0] += seconds

        return runtime.BrowserRuntime(
            FakeClient(self.docker),
            SPACE,
            "0-3",
            image,
            architecture=architecture,
            clock=lambda: self.now[0],
            sleep=sleep,
        )

    def browsers(self):
        return [container for container in self.docker.containers_by_id.values() if container is not self.docker.egress]

    def test_the_first_task_cleans_then_creates_one_hardened_browser_on_a_fresh_generation(self):
        lease = self.runtime.acquire("team_1", "pt-BR")
        self.assertEqual(self.docker.egress.restarts, 1)
        self.assertEqual(lease, runtime.Lease("team_1", "browser-1", "172.30.0.9", _generation(1)))
        [created] = self.docker.created
        seccomp = created["security_opt"][1]
        self.assertTrue(seccomp.startswith("seccomp={"))
        self.assertIn('"unshare"', seccomp)
        self.assertEqual(
            created["environment"], {"BROWSER_PROXY": "172.30.0.2", "BROWSER_LANG": "pt-BR", "BROWSER_MODE": "agent"}
        )
        self.assertEqual(created["name"], f"shimpz-local-{space_prefix(SPACE)}-team_1-browser")
        self.assertEqual(created["labels"][local_labels.KIND_LABEL], "browser")
        self.assertEqual(created["labels"][local_labels.TEAM_LABEL], "team_1")
        self.assertEqual(created["restart_policy"], {"Name": "no"})
        self.assertEqual(created["tmpfs"], runtime.TMPFS)
        for value in runtime.TMPFS.values():
            self.assertTrue(value.startswith("rw,noexec,nosuid,nodev,"))
        self.runtime.release(lease)
        self.assertEqual(self.browsers(), [])
        self.assertEqual(self.docker.egress.restarts, 2)

    def test_a_person_signs_in_on_a_controlled_browser_whose_display_holds_drawn_credentials(self):
        browser = self.make()
        lease = browser.acquire("neo", "pt-BR", mode=runtime.PERSON, url="https://www.linkedin.com/login")
        created = self.docker.created[-1]
        self.assertEqual(created["environment"]["BROWSER_MODE"], "person")
        self.assertEqual(created["environment"]["BROWSER_URL"], "https://www.linkedin.com/login")
        self.assertEqual(lease.mode, runtime.PERSON)
        written = self.wait_input(("credentials",))
        self.assertEqual(written, lease.credentials.file())
        viewer, controller = written.decode().splitlines()
        self.assertTrue(viewer.startswith("viewer:$5$kasm$") and viewer.endswith(":r"))
        self.assertTrue(controller.startswith("controller:$5$kasm$") and controller.endswith(":rw"))
        browser.release(lease)

    def test_a_signed_in_browser_restores_its_capsule_before_chromium_starts(self):
        browser = self.make()
        lease = browser.acquire("neo", "en", mode=runtime.SIGNED_IN, capsule=iter((b"first", b"second")))
        self.assertEqual(self.wait_input(("profile", "import")), b"firstsecond")
        self.assertIsNotNone(lease.credentials)
        self.assertNotIn("BROWSER_URL", self.docker.created[-1]["environment"])
        browser.release(lease)

    def test_only_a_mode_with_exactly_its_own_inputs_is_created(self):
        browser = self.make()
        for options in (
            {"mode": "root"},
            {"mode": runtime.PERSON},
            {"mode": runtime.AGENT, "url": "https://example.com"},
            {"mode": runtime.SIGNED_IN},
            {"mode": runtime.PERSON, "url": "https://example.com", "capsule": iter(())},
        ):
            with self.subTest(options=options), self.assertRaises(runtime.BrowserError) as raised:
                browser.acquire("neo", "en", **options)
            self.assertEqual(raised.exception.code, "browser-unavailable")
        self.assertEqual(self.docker.created, [])

    def test_a_restore_or_credentials_the_browser_refuses_removes_it(self):
        for program in (("credentials",), ("profile", "import")):
            with self.subTest(program=program):
                self.setUp()
                self.docker.programs[program] = (1, b"")
                browser = self.make()
                with self.assertRaises(runtime.BrowserError):
                    browser.acquire("neo", "en", mode=runtime.SIGNED_IN, capsule=iter((b"capsule",)))
                self.assertEqual(self.browsers(), [])

    def test_recovery_cleans_a_dirty_runtime_unless_a_task_holds_the_browser(self):
        browser = self.make()
        self.assertTrue(browser.recover())
        browser._dirty = True
        with mock.patch.object(browser, "clean", side_effect=runtime.BrowserError("browser-lost")):
            self.assertFalse(browser.recover())
        self.assertTrue(browser.recover())
        self.assertFalse(browser._dirty)
        # A clean runtime has nothing to recover.
        with mock.patch.object(browser, "clean") as untouched:
            self.assertTrue(browser.recover())
        untouched.assert_not_called()
        lease = browser.acquire("neo", "en")
        # The task holding the Browser cleaned first: nothing is left to recover.
        self.assertTrue(browser.recover())
        browser.release(lease)

    def test_a_clean_stop_keeps_the_browser_and_any_doubt_is_a_lost_browser(self):
        browser = self.make()
        lease = browser.acquire("neo", "en", mode=runtime.PERSON, url="https://example.com")
        self.docker.programs[("stop",)] = (0, b"stopped\n")
        browser.stop(lease)
        for answer in ((1, b"stopped\n"), (0, b""), (0, b"stopped\nmore\n"), (0, b"running\n")):
            with self.subTest(answer=answer), self.assertRaises(runtime.BrowserError):
                self.docker.programs[("stop",)] = answer
                browser.stop(lease)
        browser.release(lease)

    def test_an_export_streams_the_capsule_and_refuses_a_failed_or_oversized_one(self):
        browser = self.make()
        lease = browser.acquire("neo", "en", mode=runtime.PERSON, url="https://example.com")
        self.docker.programs[("profile", "export")] = (0, b"x" * 3000)
        chunks = []
        self.assertEqual(browser.export(lease, chunks.append), 3000)
        self.assertEqual(b"".join(chunks), b"x" * 3000)
        self.docker.programs[("profile", "export")] = (1, b"partial")
        with self.assertRaises(runtime.BrowserError):
            browser.export(lease, chunks.append)
        self.docker.programs[("profile", "export")] = (0, b"x" * 3000)
        with mock.patch.object(runtime, "MAX_CAPSULE_BYTES", 2999), self.assertRaises(runtime.BrowserError):
            browser.export(lease, chunks.append)
        browser.release(lease)

    def test_a_program_docker_cannot_carry_fails_as_unavailable_or_lost(self):
        browser = self.make()
        self.assertEqual(browser.image, IMAGE)
        lease = browser.acquire("neo", "en", mode=runtime.PERSON, url="https://example.com")
        with mock.patch.object(self.docker.api, "exec_inspect", side_effect=APIError("inspect failed")):
            with self.assertRaises(runtime.BrowserError) as fed:
                browser._feed(lease.container_id, ["credentials"], (b"x",))
            self.assertEqual(fed.exception.code, "browser-unavailable")
            with self.assertRaises(runtime.BrowserError) as exported:
                browser.export(lease, lambda _chunk: None)
            self.assertEqual(exported.exception.code, "browser-lost")
        # An opening Docker never answers is no longer waited for once the program's deadline passed.
        opening = threading.Event()
        self.addCleanup(opening.set)

        def stalled(*_args):
            # Docker takes past the program's whole deadline, and longer still.
            self.now[0] += 60
            opening.wait(30)
            raise runtime.BrowserError("browser-lost")

        with (
            mock.patch.object(browser, "_exec", side_effect=stalled),
            mock.patch.object(runtime, "STOP_SECONDS", 0.2),
            self.assertRaises(runtime.BrowserError) as unopened,
        ):
            browser.stop(lease)
        self.assertEqual(unopened.exception.code, "browser-lost")
        # A program Docker cannot even start fails as its own operation does.
        with (
            mock.patch.object(browser, "_exec", side_effect=runtime.BrowserError("browser-lost")),
            self.assertRaises(runtime.BrowserError) as unstarted,
        ):
            browser._feed(lease.container_id, ["credentials"], (b"x",))
        self.assertEqual(unstarted.exception.code, "browser-unavailable")
        # An inspection that never answers cannot hold a Save past the program's own deadline.
        stalled = threading.Event()
        self.addCleanup(stalled.set)
        self.docker.programs[("stop",)] = (0, b"stopped\n")
        with (
            mock.patch.object(self.docker.api, "exec_inspect", side_effect=lambda _id: stalled.wait(30)),
            mock.patch.object(runtime, "STOP_SECONDS", 0.2),
            self.assertRaises(runtime.BrowserError) as stopped,
        ):
            browser.stop(lease)
        self.assertEqual(stopped.exception.code, "browser-lost")
        browser.release(lease)

    def wait_input(self, program):
        for _ in range(200):
            if self.docker.inputs.get(program):
                time.sleep(0.05)
                return bytes(self.docker.inputs[program])
            time.sleep(0.01)
        return bytes(self.docker.inputs.get(program, b""))

    def test_a_second_task_never_shares_the_generation_an_earlier_one_used(self):
        first = self.runtime.acquire("team_1", "en")
        self.runtime.release(first)
        second = self.runtime.acquire("team_2", "en")
        self.assertNotEqual(first.egress, second.egress)
        self.assertEqual(self.docker.egress.restarts, 2)
        self.runtime.release(second)
        # A generation an earlier task used, still running, is restarted before the next task.
        self.runtime._used = runtime.BrowserRuntime._generation(self.runtime, self.docker.egress)
        third = self.runtime.acquire("team_1", "en")
        self.assertEqual(self.docker.egress.restarts, 4)
        self.assertEqual(third.egress, _generation(4))

    def test_a_restart_waits_for_the_new_processs_own_probe(self):
        # Docker shows the old process's probe for a while after the restart; only the new one's counts.
        self.docker.probe_lag = 3
        lease = self.runtime.acquire("team_1", "en")
        self.assertEqual(lease.egress, _generation(1))
        self.assertGreater(self.now[0], 0)
        self.runtime.release(lease)

    def test_a_lease_is_intact_only_on_its_own_running_browser_and_egress_process(self):
        lease = self.runtime.acquire("team_1", "en")
        self.assertTrue(self.runtime.intact(lease))
        self.docker.egress.restart(timeout=5)
        self.assertFalse(self.runtime.intact(lease))
        self.runtime.release(lease)
        lease = self.runtime.acquire("team_1", "en")
        self.docker.containers_by_id[lease.container_id].attrs["State"]["Running"] = False
        self.assertFalse(self.runtime.intact(lease))
        self.runtime.kill(lease)
        self.assertNotIn(lease.container_id, self.docker.containers_by_id)
        self.assertFalse(self.runtime.intact(lease))
        self.runtime.release(lease)
        lease = self.runtime.acquire("team_1", "en")
        self.docker.fail["get"] = True
        self.assertFalse(self.runtime.intact(lease))

    def test_a_probe_that_failed_since_leaves_the_lease_on_its_egress_process(self):
        lease = self.runtime.acquire("team_1", "en")
        health = self.docker.egress.attrs["State"]["Health"]
        # A slow probe on a loaded host fails while Docker still keeps the egress healthy: the same process runs.
        health["Log"] = [*health["Log"], {"ExitCode": -1, "Output": "Health check exceeded timeout (3s)"}]
        self.assertTrue(self.runtime.intact(lease))
        # Once its retries run out, the egress is unhealthy and the task's Browser lost.
        health["Status"] = "unhealthy"
        self.assertFalse(self.runtime.intact(lease))

    def test_one_browser_per_space_and_a_second_task_is_busy(self):
        lease = self.runtime.acquire("team_1", "en")
        with self.assertRaises(runtime.BrowserError) as raised:
            self.runtime.acquire("team_2", "en")
        self.assertEqual(raised.exception.code, "browser-busy")
        self.runtime.release(lease)

    def test_a_surviving_browser_or_another_network_member_refuses_every_team(self):
        self.runtime.acquire("team_1", "en")
        self.runtime._slot.release()  # a Team that crashed without releasing: its Browser survives
        self.runtime._dirty = False
        with self.assertRaises(runtime.BrowserError) as raised:
            self.runtime.acquire("team_2", "en")
        self.assertEqual(raised.exception.code, "browser-busy")
        # The refusal leaves the runtime dirty: the next attempt cleans first and then proceeds.
        lease = self.runtime.acquire("team_2", "en")
        self.assertEqual(lease.team_id, "team_2")
        self.runtime.release(lease)
        self.docker.extra_members = {"stranger": {}}
        with self.assertRaises(runtime.BrowserError) as raised:
            self.runtime.acquire("team_1", "en")
        self.assertEqual(raised.exception.code, "browser-busy")

    def test_an_unavailable_browser_refuses_before_any_docker_call(self):
        for case in (self.make(image=None), self.make(architecture="arm64")):
            with self.subTest(case=case), self.assertRaises(runtime.BrowserError) as raised:
                case.acquire("team_1", "en")
            self.assertEqual(raised.exception.code, "browser-unavailable")
        for language in ("", "pt_BR", "EN", "en-us", "--lang"):
            with self.subTest(language=language), self.assertRaises(runtime.BrowserError):
                self.runtime.acquire("team_1", language)
        self.assertEqual(self.docker.created, [])

    def test_where_no_browser_can_run_a_cleanup_only_proves_none_is_left(self):
        # Without a usable Browser no task has an egress generation, so a reset or start never needs the egress.
        unavailable = self.make(image=None)
        self.docker.fail["restart"] = True
        unavailable.clean()
        self.assertEqual(self.docker.egress.restarts, 0)

    def test_the_network_and_egress_must_be_exactly_the_owned_isolated_ones(self):
        breaks = {
            "internal": lambda: self.docker.network.attrs.update(Internal=False),
            "ipv6": lambda: self.docker.network.attrs.update(EnableIPv6=True),
            "gateway": lambda: self.docker.network.attrs.update(Options={}),
            "missing network": lambda: self.docker.fail.update(network=True),
            "egress labels": lambda: self.docker.egress.attrs["Config"]["Labels"].update(
                {local_labels.SPACE_LABEL: "other"}
            ),
            "egress gone": lambda: self.docker.fail.update(get=True),
            "unhealthy": lambda: setattr(self.docker, "health_after_restart", "starting"),
            "restart": lambda: self.docker.fail.update(restart=True),
            "list": lambda: self.docker.fail.update(list=True),
            "egress address": lambda: self.docker.egress.attrs["NetworkSettings"]["Networks"].update(
                {runtime.NETWORK: {}}
            ),
            "probe missing": lambda: (
                self.docker.egress.attrs["State"]["Health"].update(Log=None) or setattr(self.docker, "probe_lag", 10**6)
            ),
            "probe failed": lambda: (
                self.docker.egress.attrs["State"]["Health"].update(Log=_probe(0, code=1))
                or setattr(self.docker, "probe_lag", 10**6)
            ),
            "probe output": lambda: (
                self.docker.egress.attrs["State"]["Health"].update(Log=[{"ExitCode": 0, "Output": "healthy\n"}])
                or setattr(self.docker, "probe_lag", 10**6)
            ),
            "stale probe": lambda: setattr(self.docker, "probe_lag", 10**6),
        }
        for name, broken in breaks.items():
            with self.subTest(name=name):
                self.setUp()
                broken()
                with self.assertRaises(runtime.BrowserError) as raised:
                    self.runtime.acquire("team_1", "en")
                self.assertEqual(raised.exception.code, "browser-unavailable")
                self.assertTrue(self.runtime._dirty)
                self.assertTrue(self.runtime._slot.acquire(blocking=False))

    def test_a_published_image_is_pulled_by_digest_and_a_developer_image_never_is(self):
        del self.docker.images[PUBLISHED]
        published = self.make(image=PUBLISHED)
        published.release(published.acquire("team_1", "en"))
        self.assertEqual(self.docker.pulled, [PUBLISHED])
        del self.docker.images[IMAGE]
        with self.assertRaises(runtime.BrowserError) as raised:
            self.runtime.acquire("team_1", "en")
        self.assertEqual(raised.exception.code, "browser-unavailable")
        for failure in ("pull", "image-get"):
            with self.subTest(failure=failure):
                self.setUp()
                del self.docker.images[PUBLISHED]
                self.docker.fail[failure] = True
                with self.assertRaises(runtime.BrowserError):
                    self.make(image=PUBLISHED).acquire("team_1", "en")

    def test_a_created_browser_that_drifts_resolves_another_image_or_never_gets_ready_is_removed(self):
        cases = {
            "image mismatch": lambda: self.docker.fail.update({"image-mismatch": True}),
            "capability": lambda: self.docker.drift.update(CapAdd=["NET_ADMIN"]),
            "bind": lambda: self.docker.drift.update(Binds=["/:/host"]),
            "memory": lambda: self.docker.drift.update(Memory=1),
            "seccomp": lambda: self.docker.drift.update(SecurityOpt=["no-new-privileges:true"]),
            "tmpfs": lambda: self.docker.drift.update(Tmpfs={str(PurePosixPath("/") / "tmp"): "rw"}),
            "not ready": lambda: self.docker.programs.update({("ready",): (1, b"")}),
            "second network": lambda: setattr(
                self.docker,
                "on_start",
                lambda container: container.attrs["NetworkSettings"]["Networks"].update(other={}),
            ),
        }
        for name, cause in cases.items():
            with self.subTest(name=name):
                self.setUp()
                cause()
                with self.assertRaises(runtime.BrowserError) as raised:
                    self.runtime.acquire("team_1", "en")
                self.assertEqual(raised.exception.code, "browser-unavailable")
                self.assertEqual(self.browsers(), [])
        self.setUp()
        self.docker.fail["create"] = True
        with self.assertRaises(runtime.BrowserError):
            self.runtime.acquire("team_1", "en")

    def test_a_removal_is_proved_or_the_slot_stays_dirty(self):
        lease = self.runtime.acquire("team_1", "en")
        self.docker.fail["survive"] = True
        with self.assertRaises(runtime.BrowserError) as raised:
            self.runtime.release(lease)
        self.assertEqual(raised.exception.code, "browser-lost")
        self.assertTrue(self.runtime._dirty)
        self.docker.fail = {"remove": True}
        with self.assertRaises(runtime.BrowserError) as raised:
            self.runtime.clean()
        self.assertEqual(raised.exception.code, "browser-lost")
        self.docker.fail = {}
        self.runtime.clean()
        self.assertEqual(self.browsers(), [])
        self.assertFalse(self.runtime._dirty)

    def test_a_removal_under_way_elsewhere_is_proved_like_any_other(self):
        lease = self.runtime.acquire("team_1", "en")
        # Stop's removal and the task's release race: Docker refuses the second, which waits for the first's end.
        self.docker.removing.add(lease.container_id)
        self.docker.containers_by_id.pop(lease.container_id)
        self.docker.lingering[lease.container_id] = 3
        self.docker.gone[lease.container_id] = object()
        self.runtime.release(lease)
        self.assertFalse(self.runtime._dirty)
        self.assertEqual(self.docker.lingering[lease.container_id], 0)

    def test_a_removed_browser_still_listed_is_checked_until_it_is_gone_or_the_deadline(self):
        lease = self.runtime.acquire("team_1", "en")
        self.docker.linger = 5
        started = self.now[0]
        self.runtime.release(lease)
        self.assertFalse(self.runtime._dirty)
        self.assertAlmostEqual(self.now[0] - started, 5 * runtime.REMOVE_POLL_SECONDS)
        # One that never goes hung: it is lost once the one deadline passed.
        lease = self.runtime.acquire("team_1", "en")
        self.docker.linger = 10**6
        started = self.now[0]
        with self.assertRaises(runtime.BrowserError) as raised:
            self.runtime.release(lease)
        self.assertEqual(raised.exception.code, "browser-lost")
        self.assertTrue(self.runtime._dirty)
        self.assertAlmostEqual(self.now[0] - started, runtime.REMOVE_SECONDS, delta=runtime.REMOVE_POLL_SECONDS)

    def stalled_release(self, failure: str) -> None:
        lease = self.runtime.acquire("team_1", "en")
        stalled = threading.Event()
        self.addCleanup(stalled.set)
        self.docker.fail[failure] = stalled
        with mock.patch.object(runtime, "REMOVE_SECONDS", 0.2), self.assertRaises(runtime.BrowserError) as raised:
            self.runtime.release(lease)
        self.assertEqual(raised.exception.code, "browser-lost")
        self.assertTrue(self.runtime._dirty)

    def test_a_stalled_removal_never_outlasts_its_deadline(self):
        self.stalled_release("stall-remove")

    def test_a_stalled_check_never_outlasts_the_removal_deadline(self):
        self.stalled_release("stall-get")

    def test_a_removal_that_cannot_be_checked_is_lost_and_an_absent_one_is_success(self):
        self.runtime._remove("never-existed")
        self.docker.fail["get"] = True
        with self.assertRaises(runtime.BrowserError) as raised:
            self.runtime._remove("never-existed")
        self.assertEqual(raised.exception.code, "browser-lost")

    def test_a_team_teardown_removes_only_that_teams_browser(self):
        lease = self.runtime.acquire("team_1", "en")
        self.runtime.remove_team("team_2")
        self.assertEqual(len(self.browsers()), 1)
        self.runtime.remove_team("team_1")
        self.assertEqual(self.browsers(), [])
        self.runtime._slot.release()
        del lease

    def test_the_downloads_tool_answers_are_strictly_framed(self):
        lease = self.runtime.acquire("team_1", "en")
        listing = {"files": [{"name": "edital.pdf", "size": 9}], "refused": [], "skipped": 0}
        self.docker.programs[("files", "list")] = (0, json.dumps(listing).encode() + b"\n")
        self.docker.programs[("files", "read", "edital.pdf")] = (0, b"9\n%PDF-1.4\n")
        self.assertEqual(self.runtime.files(lease), listing)
        self.assertEqual(self.runtime.read(lease, "edital.pdf", 9), b"%PDF-1.4\n")
        for output in (b"[]\n", b'{"files": []}\n', b"not json\n", b'{"files":[]}'):
            with self.subTest(output=output), self.assertRaises(runtime.BrowserError) as raised:
                self.docker.programs[("files", "list")] = (0, output)
                self.runtime.files(lease)
            self.assertEqual(raised.exception.code, "browser-lost")
        for output in (b"8\n%PDF-1.4\n", b"9\nshort", b"9\n%PDF-1.4\nextra"):
            with self.subTest(output=output), self.assertRaises(runtime.BrowserError):
                self.docker.programs[("files", "read", "edital.pdf")] = (0, output)
                self.runtime.read(lease, "edital.pdf", 9)
        self.runtime.release(lease)

    def test_the_relay_and_the_bounded_proof_its_exec_ended(self):
        lease = self.runtime.acquire("team_1", "en")
        exec_id, stream = self.runtime.open_relay(lease)
        self.assertEqual(self.docker.api.execs[exec_id][1], ("relay",))
        stream.close()
        stream.close()
        # An exec Docker created but gave no channel to names itself, so its end can still be proven.
        for failure in ("start", "no-socket"):
            with self.subTest(failure=failure):
                self.docker.fail = {failure: True}
                with self.assertRaises(runtime.ExecStartError) as raised:
                    self.runtime.open_relay(lease)
                self.assertEqual(raised.exception.code, "browser-lost")
                self.assertEqual(self.docker.api.execs[raised.exception.exec_id][1], ("relay",))
        self.docker.fail = {}
        answers = iter([{"Running": True}, {"Running": False}])
        self.docker.api.exec_inspect = lambda _exec: next(answers)
        self.assertTrue(self.runtime.exited(exec_id, self.now[0] + 1))
        self.docker.api.exec_inspect = lambda _exec: {"Running": True}
        self.assertFalse(self.runtime.exited(exec_id, self.now[0] + 0.3))
        self.assertFalse(self.runtime.exited(exec_id, self.now[0]))

        def fails(_exec):
            raise APIError("inspect failed")

        self.docker.api.exec_inspect = fails
        self.assertFalse(self.runtime.exited(exec_id, self.now[0] + 0.2))
        # A stalled inspection proves nothing, and its late answer is discarded.
        stalled = threading.Event()
        self.addCleanup(stalled.set)
        self.docker.api.exec_inspect = lambda _exec: stalled.wait() and {"Running": False}
        self.assertFalse(self.runtime.exited(exec_id, self.now[0] + 0.2))
        self.runtime.release(lease)

    def test_the_mcp_stream_and_program_failures(self):
        lease = self.runtime.acquire("team_1", "en")
        exec_id, stream = self.runtime.open_stream(lease, self.now[0] + 10, lambda: True)
        self.assertTrue(exec_id)
        self.assertIsNotNone(stream)
        self.assertEqual(self.runtime.program(lease.container_id, ["ready"], deadline=10)[0], 0)
        self.docker.programs[("ready",)] = (0, b"unexpected")
        self.assertEqual(self.runtime.program(lease.container_id, ["ready"], deadline=10)[0], -1)
        for failure in ("exec", "no-socket"):
            with self.subTest(failure=failure):
                self.docker.fail = {failure: True}
                self.assertEqual(self.runtime.program(lease.container_id, ["ready"], deadline=10)[0], -1)
                with self.assertRaises(runtime.BrowserError):
                    self.runtime.open_stream(lease, self.now[0] + 10, lambda: True)
        self.docker.fail = {}
        self.docker.api.exec_inspect = lambda _exec: {"ExitCode": None}
        self.assertEqual(self.runtime.program(lease.container_id, ["ready"], deadline=10)[0], -1)
        self.runtime.release(lease)

    def test_a_stalled_driver_session_ends_at_stop_and_opens_too_late_unused(self):
        lease = self.runtime.acquire("team_1", "en")
        stalled, stop = threading.Event(), threading.Event()
        self.addCleanup(stalled.set)
        self.docker.fail = {"stall-start": stalled}
        timer = threading.Timer(0.2, stop.set)
        timer.start()
        self.addCleanup(timer.cancel)
        started = time.monotonic()
        # The task's deadline is far: Stop alone ends the wait.
        with self.assertRaises(runtime.BrowserError) as raised:
            self.runtime.open_stream(lease, self.now[0] + 3600, lambda: not stop.is_set())
        self.assertEqual(raised.exception.code, "browser-lost")
        self.assertLess(time.monotonic() - started, 2)
        # Docker opens it after the deadline: the session's channel is closed before anything reaches it.
        opened = len(self.docker.sockets)
        stalled.set()
        deadline = time.monotonic() + 5
        while len(self.docker.sockets) == opened and time.monotonic() < deadline:
            time.sleep(0.01)
        team = self.docker.sockets[opened]
        while team.fileno() != -1 and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertEqual(team.fileno(), -1)
        self.docker.fail = {}
        self.runtime.release(lease)

    def test_a_driver_session_that_fails_after_its_wait_ended_leaves_nothing_open(self):
        lease = self.runtime.acquire("team_1", "en")
        stalled = threading.Event()
        self.addCleanup(stalled.set)
        self.docker.fail = {"stall-start": stalled, "start": True}
        with self.assertRaises(runtime.BrowserError):
            self.runtime.open_stream(lease, self.now[0] + 3600, lambda: False)
        opened = len(self.docker.sockets)
        stalled.set()
        deadline = time.monotonic() + 5
        while any(thread.name == "browser-driver" for thread in threading.enumerate()) and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertEqual(len(self.docker.sockets), opened)
        self.docker.fail = {}
        self.runtime.release(lease)

    def test_the_team_reads_its_browser_from_its_own_image_and_starts_clean(self):
        team = FakeContainer(
            self.docker,
            "team-id",
            "shimpz-team",
            {"Image": "sha256:team", "Config": {"Labels": {}}, "NetworkSettings": {"Networks": {}}},
        )
        self.docker.containers_by_id[team.id] = team
        image = type("Image", (), {"attrs": {"Config": {"Labels": {runtime.IMAGE_LABEL: IMAGE}}}})()
        client = FakeClient(self.docker)
        client.images = type("Images", (), {"get": staticmethod(lambda _ref: image)})()
        with mock.patch.object(runtime.socket, "gethostname", return_value="team-id"):
            self.assertEqual(runtime.own_browser_image(client), IMAGE)
        with mock.patch.object(runtime.socket, "gethostname", return_value="missing"):
            self.assertIsNone(runtime.own_browser_image(client))
        self.assertTrue(self.runtime.available)
        self.assertFalse(self.make(image=None).available)
        # A leftover Browser is removed at startup, and the egress the Space is starting is left alone.
        leftover = self.runtime.acquire("team_1", "en")
        self.runtime._slot.release()
        restarts = self.docker.egress.restarts
        self.assertTrue(self.runtime.startup())
        self.assertNotIn(leftover.container_id, self.docker.containers_by_id)
        self.assertEqual(self.docker.egress.restarts, restarts)
        self.docker.fail["list"] = True
        self.assertFalse(self.runtime.startup())
        self.docker.fail = {}
        # The runtime stays dirty after startup, so the first task still starts on a fresh egress generation.
        self.runtime._dirty = True
        lease = self.runtime.acquire("team_1", "en")
        self.assertEqual(self.docker.egress.restarts, restarts + 1)
        self.assertFalse(self.runtime.startup())
        self.runtime.release(lease)

    def test_a_team_image_names_its_browser_only_by_exact_digest(self):
        self.assertEqual(runtime.browser_image({runtime.IMAGE_LABEL: IMAGE}), IMAGE)
        self.assertEqual(runtime.browser_image({runtime.IMAGE_LABEL: PUBLISHED}), PUBLISHED)
        for labels in (
            None,
            {},
            {runtime.IMAGE_LABEL: "localhost/shimpz-browser:latest"},
            {runtime.IMAGE_LABEL: "docker.io/shimpz-browser@sha256:" + "a" * 64},
            {runtime.IMAGE_LABEL: 7},
        ):
            with self.subTest(labels=labels):
                self.assertIsNone(runtime.browser_image(labels))


if __name__ == "__main__":
    unittest.main()
