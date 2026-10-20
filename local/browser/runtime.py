"""The Local Team Browser's Docker lifecycle (ADR-0117 §4 and §8).

One Browser exists per Space at a time. Before creating one, Team proves from the labelled Docker inventory that no
Browser container exists and that the egress is the static Browser network's only member, and it runs every task on a
fresh egress generation: the egress is restarted after each task, and before a task whenever the generation an earlier
task used is still running. A failed or unproven removal keeps every later task refused until a cleanup proves the
Space clean again.
"""

from __future__ import annotations

import contextlib
import json
import re
import socket
import threading
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from docker.errors import APIError, DockerException, NotFound
from docker.types import LogConfig, Ulimit

from local import labels as local_labels
from local.browser.channel import ChannelError, ExecStream
from local.browser.credentials import Credentials
from local.validation import space_prefix

KIND = "browser"
EGRESS_KIND = "browser-egress"
NETWORK = "shimpz-space-browser"
EGRESS = "shimpz-browser-egress"
IMAGE_LABEL = "org.shimpz.local.browser-image"
PUBLISHED = "ghcr.io/theshimpz/shimpz-browser"
IMAGE_RE = re.compile(rf"(?:localhost/shimpz-browser|{re.escape(PUBLISHED)})@sha256:[0-9a-f]{{64}}")
USER = "10031:10031"
MEMORY = 2 * 1024 * 1024 * 1024
NANO_CPUS = 1_000_000_000
# How often a Driver session's opening checks that it is still wanted.
DRIVER_POLL_SECONDS = 0.1
PIDS = 512
NOFILE = 4096
SHM = 256 * 1024 * 1024
_OWNER = "uid=10031,gid=10031,mode=0700"
# Every writable path of the Browser is one of these; nothing on them executes (ADR-0117 §3.3).
TMPFS = {
    str(PurePosixPath("/") / "tmp"): f"rw,noexec,nosuid,nodev,size=64m,nr_inodes=4096,{_OWNER}",
    "/profile": f"rw,noexec,nosuid,nodev,size=256m,nr_inodes=16384,{_OWNER}",
    "/downloads": f"rw,noexec,nosuid,nodev,size=256m,nr_inodes=256,{_OWNER}",
}
HOST = "/usr/local/bin/browser-host"
READY_SECONDS = 20.0
# Removing a Browser, proved absent, ends by this one deadline; its absence is checked again at this interval.
REMOVE_SECONDS = 30.0
REMOVE_POLL_SECONDS = 0.1
EGRESS_SECONDS = 30.0
PROGRAM_SECONDS = 30.0
# Who drives a Browser (ADR-0117, Sign-ins amendment): the agent alone, a person signing in, or a task restored from a
# Sign-in, which a person may take over. Only the last two hold display credentials and keep a profile.
AGENT, PERSON, SIGNED_IN = "agent", "person", "signed-in"
# Chromium's clean stop, and the capsule's export, each bounded (`browser-host stop`, `profile export`).
STOP_SECONDS = 40.0
CAPSULE_SECONDS = 30.0
# The longest export: the whole archive, headers, padding, and end blocks included, is at most 128 MiB (item 6).
MAX_CAPSULE_BYTES = 128 * 1024 * 1024
# The capsule crosses in chunks of this size, so Team never holds more than one at a time.
CHUNK_BYTES = 1024 * 1024
SECCOMP = Path(__file__).resolve().parents[2] / "browser" / "image" / "seccomp.json"
LANGUAGE_RE = re.compile(r"[a-z]{2}(?:-[A-Z]{2})?")
# What the egress healthcheck prints: the random generation of the process that accepted its connection.
GENERATION_RE = re.compile(r"([0-9a-f]{32})\n?")


class BrowserError(RuntimeError):
    """A closed reason no Browser task can run or continue: `browser-unavailable`, `browser-busy`, `browser-lost`."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class ExecStartError(BrowserError):
    """Docker created the exec, but no channel to it came back: it may run, so only proving it ended settles it."""

    def __init__(self, exec_id: str) -> None:
        super().__init__("browser-lost")
        self.exec_id = exec_id


@dataclass(frozen=True, slots=True)
class Generation:
    """One egress process: its container, the start Docker recorded, and the random id its audit lines carry."""

    container_id: str
    started: str
    generation: str


@dataclass(frozen=True, slots=True)
class Lease:
    """One running Browser: its Team, container, address, the egress generation it runs on, and who drives it."""

    team_id: str
    container_id: str
    address: str
    egress: Generation
    mode: str = AGENT
    # The display's viewer credentials, for a Browser a person may control.
    credentials: Credentials | None = None


def browser_image(labels: object) -> str | None:
    """The exact Browser image a Team image's labels name, or None (ADR-0117 §9)."""
    value = labels.get(IMAGE_LABEL) if isinstance(labels, dict) else None
    return value if isinstance(value, str) and IMAGE_RE.fullmatch(value) else None


def own_browser_image(client) -> str | None:
    """The Browser image this Team container's own image names, or None: Team then answers browser-unavailable."""
    try:
        container = client.containers.get(socket.gethostname())
        image = client.images.get(container.attrs["Image"])
    except DockerException, KeyError, TypeError:
        return None
    attrs = image.attrs if isinstance(image.attrs, dict) else {}
    return browser_image((attrs.get("Config") or {}).get("Labels"))


def _bounded(call: Callable[[], object], seconds: float) -> object | None:
    """`call`'s answer if it came within `seconds`; its failure, or an answer after them, is None and is discarded."""
    answer: list[object] = []

    def run() -> None:
        with contextlib.suppress(Exception):
            answer.append(call())

    worker = threading.Thread(target=run, name="browser-inspect", daemon=True)
    worker.start()
    worker.join(seconds)
    return answer[0] if answer and not worker.is_alive() else None


def _attempt(call: Callable[[], object], seconds: float) -> tuple[bool, object] | None:
    """`call`'s answer as (True, answer) or its Docker failure as (False, error) within `seconds`; else None."""
    outcome: list[tuple[bool, object]] = []

    def run() -> None:
        try:
            outcome.append((True, call()))
        except DockerException as exc:
            outcome.append((False, exc))

    worker = threading.Thread(target=run, name="browser-remove", daemon=True)
    worker.start()
    worker.join(max(seconds, 0.0))
    return outcome[0] if outcome and not worker.is_alive() else None


def _removal_under_way(failure: object) -> bool:
    """Whether Docker refused a removal because another one of the same container is already under way."""
    return isinstance(failure, APIError) and failure.status_code == 409


def _network_address(networks: dict) -> str:
    address = (networks.get(NETWORK) or {}).get("IPAddress")
    if not isinstance(address, str) or not address:
        raise BrowserError("browser-unavailable")
    return address


class BrowserRuntime:
    """Admission, creation, the exec channels, and proven removal of the Space's one Browser."""

    def __init__(
        self,
        client,
        space_id: str,
        cpuset: str,
        image: str | None,
        *,
        architecture: str = "amd64",
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._client = client
        self._space_id = space_id
        self._cpuset = cpuset
        self._image = image if architecture == "amd64" else None
        self._clock = clock
        self._sleep = sleep
        self._slot = threading.Lock()
        # The egress generation an earlier task ran on, which no later task may share; unknown after a Team start.
        self._used: Generation | None = None
        self._dirty = True

    @property
    def image(self) -> str | None:
        """The exact Browser image this Team runs, which a Sign-in records as its writer."""
        return self._image

    @property
    def available(self) -> bool:
        """Whether this Team image names a Browser it can run: an exact image on amd64."""
        return self._image is not None

    # Admission and creation

    def acquire(
        self,
        team_id: str,
        language: str,
        *,
        mode: str = AGENT,
        url: str | None = None,
        capsule: Iterable[bytes] | None = None,
    ) -> Lease:
        """Create this task's Browser on a fresh egress generation; the caller releases the returned lease.

        A person's sign-in starts at `url`; a signed-in Browser restores the capsule's chunks before Chromium starts.
        """
        if (
            self._image is None
            or not LANGUAGE_RE.fullmatch(language)
            or mode not in {AGENT, PERSON, SIGNED_IN}
            or (url is not None) != (mode == PERSON)
            or (capsule is not None) != (mode == SIGNED_IN)
        ):
            raise BrowserError("browser-unavailable")
        if not self._slot.acquire(blocking=False):
            raise BrowserError("browser-busy")
        try:
            if self._dirty:
                self.clean()
            self._prove_clean()
            egress = self._fresh_egress()
            lease = self._create(team_id, language, egress, mode, url, capsule)
        except BaseException:
            self._dirty = True
            self._slot.release()
            raise
        self._used = egress
        return lease

    def release(self, lease: Lease) -> None:
        """Remove the task's Browser, prove it absent, and end its egress generation; then free the slot."""
        try:
            self._remove(lease.container_id)
            self._restart_egress()
            self._dirty = False
        except BrowserError:
            self._dirty = True
            raise
        finally:
            self._slot.release()

    def recover(self) -> bool:
        """Clean up after a removal that failed; whether no residue is left, proven.

        While a task holds the slot, only its own cleanup can prove that: until it did, nothing is proven yet.
        """
        if not self._slot.acquire(blocking=False):
            return not self._dirty
        try:
            if self._dirty:
                self.clean()
        except BrowserError:
            return False
        finally:
            self._slot.release()
        return True

    def startup(self) -> bool:
        """Remove every Browser a previous Team process left; whether it proved none is left.

        The egress is not restarted here: the Space may be starting it at this very moment, and the runtime stays dirty,
        so the first task restarts it before it runs, on a generation no earlier task used.
        """
        if not self._slot.acquire(blocking=False):
            return False
        try:
            for container in self._browsers():
                self._remove(container.id)
        except BrowserError:
            return False
        finally:
            self._slot.release()
        return True

    def clean(self) -> None:
        """Remove every Browser of this Space and restart the egress, proving both; Team startup and recovery.

        Where this Team can run no Browser, no task has an egress generation to end: proving no Browser is left is all.
        """
        for container in self._browsers():
            self._remove(container.id)
        if self.available:
            self._restart_egress()
        self._dirty = False

    def remove_team(self, team_id: str) -> None:
        """Remove one Team's Browser, as its teardown requires; an absent Browser is success."""
        for container in self._browsers(team_id):
            self._remove(container.id)

    def _browsers(self, team_id: str | None = None) -> list:
        labels = local_labels.ownership_labels(self._space_id, KIND, team_id)
        try:
            return self._client.containers.list(all=True, filters={"label": local_labels.label_filter(labels)})
        except DockerException as exc:
            raise BrowserError("browser-unavailable") from exc

    def _prove_clean(self) -> None:
        if self._browsers():
            raise BrowserError("browser-busy")
        network = self._network()
        egress = self._egress()
        members = set(network.attrs.get("Containers") or {})
        if members != {egress.id}:
            raise BrowserError("browser-busy")

    def _network(self):
        try:
            network = self._client.networks.get(NETWORK)
            network.reload()
        except DockerException as exc:
            raise BrowserError("browser-unavailable") from exc
        attrs = network.attrs
        options = attrs.get("Options") or {}
        if (
            attrs.get("Internal") is not True
            or attrs.get("EnableIPv6") is True
            or options.get("com.docker.network.bridge.gateway_mode_ipv4") != "isolated"
        ):
            raise BrowserError("browser-unavailable")
        return network

    def _egress(self):
        try:
            egress = self._client.containers.get(EGRESS)
            egress.reload()
        except DockerException as exc:
            raise BrowserError("browser-unavailable") from exc
        labels = egress.attrs.get("Config", {}).get("Labels") or {}
        expected = {
            local_labels.MANAGED_LABEL: "1",
            local_labels.SPACE_LABEL: self._space_id,
            local_labels.KIND_LABEL: EGRESS_KIND,
        }
        if not local_labels.labels_include(labels, expected):
            raise BrowserError("browser-unavailable")
        return egress

    def _generation(self, egress) -> Generation:
        """The running, healthy egress process, named by the generation its latest passing probe printed.

        A probe that failed since, a slow one on a loaded host, names no other process: Docker keeps the egress healthy
        until its retries run out, and a restart changes its start time.
        """
        state = egress.attrs.get("State") or {}
        health = state.get("Health") or {}
        started = state.get("StartedAt")
        probes = health.get("Log")
        recorded = [probe for probe in (probes if isinstance(probes, list) else ()) if isinstance(probe, dict)]
        latest = next((probe for probe in reversed(recorded) if probe.get("ExitCode") == 0), {})
        output = latest.get("Output")
        printed = GENERATION_RE.fullmatch(output) if isinstance(output, str) else None
        if (
            state.get("Running") is not True
            or health.get("Status") != "healthy"
            or not isinstance(started, str)
            or printed is None
        ):
            raise BrowserError("browser-unavailable")
        return Generation(egress.id, started, printed[1])

    def _restart_egress(self) -> Generation:
        """Restart the egress and wait for a new process's own healthy probe: a probe of the old one never counts."""
        before = self._egress()
        try:
            earlier = self._generation(before)
        except BrowserError:
            earlier = None
        try:
            before.restart(timeout=5)
        except DockerException as exc:
            raise BrowserError("browser-unavailable") from exc
        deadline = self._clock() + EGRESS_SECONDS
        while True:
            try:
                current = self._generation(self._egress())
                if earlier is None or (current.started != earlier.started and current.generation != earlier.generation):
                    return current
            except BrowserError:
                pass
            if self._clock() >= deadline:
                raise BrowserError("browser-unavailable")
            self._sleep(0.2)

    def _fresh_egress(self) -> Generation:
        generation = self._generation(self._egress())
        return self._restart_egress() if generation == self._used else generation

    def intact(self, lease: Lease) -> bool:
        """Whether the lease's Browser still runs on the very egress process it started on (ADR-0117 §4.3)."""
        try:
            current = self._generation(self._egress())
            container = self._client.containers.get(lease.container_id)
            container.reload()
        except BrowserError, DockerException:
            return False
        return current == lease.egress and (container.attrs.get("State") or {}).get("Running") is True

    def kill(self, lease: Lease) -> None:
        """Remove the lease's Browser at once, for Stop; its task's release proves the removal and frees the slot."""
        self._remove(lease.container_id)

    def _address(self, container) -> str:
        """The Browser's address on the Browser network, which must be its only network."""
        networks = (container.attrs.get("NetworkSettings") or {}).get("Networks") or {}
        if set(networks) != {NETWORK}:
            raise BrowserError("browser-unavailable")
        return _network_address(networks)

    def _create(
        self,
        team_id: str,
        language: str,
        egress: Generation,
        mode: str,
        url: str | None,
        capsule: Iterable[bytes] | None,
    ) -> Lease:
        # The egress has its outbound network too; the Browser reaches it only on the Browser network.
        proxy = _network_address((self._egress().attrs.get("NetworkSettings") or {}).get("Networks") or {})
        labels = {**local_labels.ownership_labels(self._space_id, KIND, team_id), local_labels.IMAGE_LABEL: self._image}
        try:
            image = self._client.images.get(self._image)
        except NotFound:
            if self._image.partition("@")[0] != PUBLISHED:
                raise BrowserError("browser-unavailable") from None
            image = self._pull()
        except DockerException as exc:
            raise BrowserError("browser-unavailable") from exc
        try:
            container = self._client.containers.create(
                image=self._image,
                name=self.container_name(team_id),
                detach=True,
                user=USER,
                network=NETWORK,
                labels=labels,
                environment={
                    "BROWSER_PROXY": proxy,
                    "BROWSER_LANG": language,
                    "BROWSER_MODE": mode,
                    **({"BROWSER_URL": url} if url is not None else {}),
                },
                read_only=True,
                cap_drop=["ALL"],
                security_opt=["no-new-privileges:true", f"seccomp={SECCOMP.read_text(encoding='utf-8')}"],
                privileged=False,
                init=True,
                ipc_mode="private",
                cgroupns="private",
                mem_limit=MEMORY,
                memswap_limit=MEMORY,
                nano_cpus=NANO_CPUS,
                cpuset_cpus=self._cpuset,
                pids_limit=PIDS,
                shm_size=SHM,
                tmpfs=TMPFS,
                ulimits=[Ulimit(name="nofile", soft=NOFILE, hard=NOFILE)],
                restart_policy={"Name": "no"},
                log_config=LogConfig(type=LogConfig.types.NONE),
            )
        except (DockerException, OSError) as exc:
            raise BrowserError("browser-unavailable") from exc
        try:
            container.reload()
            if container.attrs.get("Image") != image.id:
                raise BrowserError("browser-unavailable")
            container.start()
            container.reload()
            self._validate(container, team_id, labels)
            credentials = None
            if mode != AGENT:
                credentials = Credentials.draw()
                self._feed(container.id, ["credentials"], (credentials.file(),))
            if capsule is not None:
                self._feed(container.id, ["profile", "import"], capsule)
            self._wait_ready(container)
            return Lease(team_id, container.id, self._address(container), egress, mode, credentials)
        except BaseException:
            self._remove(container.id)
            raise

    def _pull(self):
        repository, digest = self._image.split("@")
        try:
            return self._client.images.pull(repository, tag=digest)
        except DockerException as exc:
            raise BrowserError("browser-unavailable") from exc

    def container_name(self, team_id: str) -> str:
        return f"shimpz-local-{space_prefix(self._space_id)}-{team_id}-browser"

    def _validate(self, container, team_id: str, labels: dict[str, str]) -> None:
        """Refuse a container whose effective configuration differs from the one requested (drift)."""
        attrs = container.attrs
        host = attrs.get("HostConfig") or {}
        config = attrs.get("Config") or {}
        tmpfs = {path: host.get("Tmpfs", {}).get(path) for path in TMPFS}
        security = host.get("SecurityOpt") or []
        if not (
            attrs.get("Name") == f"/{self.container_name(team_id)}"
            and config.get("User") == USER
            and local_labels.labels_include(config.get("Labels"), labels)
            and host.get("ReadonlyRootfs") is True
            and host.get("CapDrop") == ["ALL"]
            and not host.get("CapAdd")
            and host.get("Privileged") is False
            and len(security) == 2
            and security[0] in {"no-new-privileges", "no-new-privileges:true"}
            and str(security[1]).startswith("seccomp=")
            and host.get("Init") is True
            and host.get("IpcMode") == "private"
            and host.get("CgroupnsMode") == "private"
            and host.get("Memory") == MEMORY
            and host.get("MemorySwap") == MEMORY
            and host.get("NanoCpus") == NANO_CPUS
            and host.get("PidsLimit") == PIDS
            and host.get("NetworkMode") == NETWORK
            and not host.get("Binds")
            and not attrs.get("Mounts")
            and tmpfs == TMPFS
            and (attrs.get("State") or {}).get("Running") is True
        ):
            raise BrowserError("browser-unavailable")

    def _wait_ready(self, container) -> None:
        deadline = self._clock() + READY_SECONDS
        while self.program(container.id, ["ready"], deadline=self._clock() + 2)[0] != 0:
            if self._clock() >= deadline:
                raise BrowserError("browser-unavailable")
            self._sleep(0.2)

    def _remove(self, container_id: str) -> None:
        """Remove one Browser and prove it absent, all by one deadline that no stalled Docker call extends.

        A removal another caller already has under way (Stop and the task's release race) is no failure: its end is
        proved like any other. Only Docker answering that the container is gone proves it; a container still present
        is checked again until the deadline, which means it hung.
        """
        deadline = self._clock() + REMOVE_SECONDS
        removed = _attempt(lambda: self._client.api.remove_container(container_id, force=True), REMOVE_SECONDS)
        if removed is None:
            raise BrowserError("browser-lost")
        done, failure = removed
        if not done and not isinstance(failure, NotFound) and not _removal_under_way(failure):
            raise BrowserError("browser-lost") from failure
        while (remaining := deadline - self._clock()) > 0:
            checked = _attempt(lambda: self._client.containers.get(container_id), remaining)
            if checked is None:
                break
            present, answer = checked
            if not present:
                if isinstance(answer, NotFound):
                    return
                raise BrowserError("browser-lost") from answer
            self._sleep(REMOVE_POLL_SECONDS)
        raise BrowserError("browser-lost")

    # The exec channels

    def open_stream(self, lease: Lease, deadline: float, wanted: Callable[[], bool]) -> tuple[str, ExecStream]:
        """The Cua Driver's stdio MCP session in this Browser, opened by `deadline`: its exec id and its channel.

        The wait ends as soon as the session is no longer `wanted` (Stop).
        """
        exec_id, raw = self._open_exec(lease.container_id, ["mcp"], deadline, wanted)
        return exec_id, ExecStream(raw, self._clock)

    def _open_exec(
        self, container_id: str, arguments: list[str], deadline: float, wanted: Callable[[], bool] = lambda: True
    ) -> tuple[str, object]:
        """One `browser-host` exec opened by `deadline` while still `wanted`: its id and its socket.

        Docker's own request timeout does not bound the whole opening; this does. An exec Docker opens only after the
        wait ended is closed unused, so nothing ever reaches it.
        """
        guard = threading.Lock()
        opened: list[tuple[str, object] | BrowserError] = []
        # The wait ended without an exec, under `guard`: one Docker opens afterwards is closed unused.
        ended = False

        def run() -> None:
            try:
                answer: tuple[str, object] | BrowserError = self._exec(container_id, arguments)
            except BrowserError as exc:
                answer = exc
            with guard:
                if not ended:
                    opened.append(answer)
                    return
            if isinstance(answer, tuple):
                with contextlib.suppress(OSError):
                    answer[1].close()

        worker = threading.Thread(target=run, name="browser-exec", daemon=True)
        worker.start()
        while worker.is_alive() and (left := deadline - self._clock()) > 0 and wanted():
            worker.join(min(left, DRIVER_POLL_SECONDS))
        with guard:
            if not opened:
                ended = True
                raise BrowserError("browser-lost")
        answer = opened[0]
        if isinstance(answer, BrowserError):
            raise answer
        return answer

    def open_relay(self, lease: Lease) -> tuple[str, ExecStream]:
        """One viewer's relay into this Browser's display: its exec id and its channel."""
        exec_id, raw = self._exec(lease.container_id, ["relay"])
        return exec_id, ExecStream(raw, self._clock)

    def exited(self, exec_id: str, deadline: float) -> bool:
        """Whether Docker proved the exec ended before `deadline`; a stalled or failed inspection proves nothing."""
        while (remaining := deadline - self._clock()) > 0:
            details = _bounded(lambda: self._client.api.exec_inspect(exec_id), remaining)
            if isinstance(details, dict) and details.get("Running") is False:
                return True
            self._sleep(0.1)
        return False

    def program(self, container_id: str, arguments: list[str], *, deadline: float) -> tuple[int, ExecStream | None]:
        """Run one bounded `browser-host` program; its exit status once its output was read, and its stream."""
        try:
            exec_id, raw = self._exec(container_id, arguments)
            stream = ExecStream(raw, self._clock)
            stream.close_input()
            stream.receive_end(deadline)
            details = self._client.api.exec_inspect(exec_id)
        except BrowserError, DockerException, ChannelError, OSError:
            return -1, None
        code = details.get("ExitCode")
        return (code if isinstance(code, int) else -1), stream

    def _bounded_program[T](
        self, container_id: str, arguments: list[str], seconds: float, code: str, body: Callable[[ExecStream, float], T]
    ) -> T:
        """Run one `browser-host` program for `body` to speak with, all of it before one deadline `seconds` away.

        Its exit status 0 must be proven by that deadline too; the channel is closed however it ends, and any failure
        or doubt is `code`.
        """
        deadline = self._clock() + seconds
        try:
            exec_id, raw = self._open_exec(container_id, arguments, deadline)
        except BrowserError as exc:
            raise BrowserError(code) from exc
        stream = ExecStream(raw, self._clock)
        try:
            result = body(stream, deadline)
            details = _bounded(lambda: self._client.api.exec_inspect(exec_id), deadline - self._clock())
        except (DockerException, ChannelError, OSError) as exc:
            raise BrowserError(code) from exc
        finally:
            stream.close()
        if not isinstance(details, dict) or details.get("ExitCode") != 0:
            raise BrowserError(code)
        return result

    def _feed(self, container_id: str, arguments: list[str], chunks: Iterable[bytes]) -> None:
        """Run one `browser-host` program on `chunks` as its input; it must end with status 0."""

        def send(stream: ExecStream, deadline: float) -> None:
            for chunk in chunks:
                stream.send(chunk, deadline)
            stream.close_input()
            stream.receive_end(deadline)

        self._bounded_program(container_id, arguments, CAPSULE_SECONDS, "browser-unavailable", send)

    def stop(self, lease: Lease) -> None:
        """End the Browser's Chromium session cleanly, keeping its container for the export; any doubt is a failure."""

        def stopped(stream: ExecStream, deadline: float) -> bytes:
            stream.close_input()
            answer = stream.receive_line(deadline, 16)
            stream.receive_end(deadline)
            return answer

        if self._bounded_program(lease.container_id, ["stop"], STOP_SECONDS, "browser-lost", stopped) != b"stopped":
            raise BrowserError("browser-lost")

    def export(self, lease: Lease, sink: Callable[[bytes], None]) -> int:
        """Hand the stopped Browser's capsule to `sink` as it arrives; its size, once the export ended with status 0."""

        def exported(stream: ExecStream, deadline: float) -> int:
            stream.close_input()
            total = 0
            while chunk := stream.receive_chunk(CHUNK_BYTES, deadline):
                total += len(chunk)
                if total > MAX_CAPSULE_BYTES:
                    raise BrowserError("browser-lost")
                sink(chunk)
            return total

        arguments = ["profile", "export"]
        return self._bounded_program(lease.container_id, arguments, CAPSULE_SECONDS, "browser-lost", exported)

    def files(self, lease: Lease) -> dict[str, object]:
        """The downloads listing `browser-host files list` returns, strictly parsed."""
        stream = ExecStream(self._exec(lease.container_id, ["files", "list"])[1], self._clock)
        stream.close_input()
        deadline = self._clock() + PROGRAM_SECONDS
        try:
            listing = json.loads(stream.receive_line(deadline, 256 * 1024))
            stream.receive_end(deadline)
        except (ChannelError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise BrowserError("browser-lost") from exc
        if not isinstance(listing, dict) or set(listing) != {"files", "refused", "skipped"}:
            raise BrowserError("browser-lost")
        return listing

    def read(self, lease: Lease, name: str, size: int) -> bytes:
        """One listed download's exact bytes, framed by the length the program states first."""
        stream = ExecStream(self._exec(lease.container_id, ["files", "read", name])[1], self._clock)
        stream.close_input()
        deadline = self._clock() + PROGRAM_SECONDS
        try:
            stated = stream.receive_line(deadline, 20)
            if stated != str(size).encode():
                raise BrowserError("browser-lost")
            data = stream.receive_exact(size, deadline)
            stream.receive_end(deadline)
        except ChannelError as exc:
            raise BrowserError("browser-lost") from exc
        return data

    def _exec(self, container_id: str, arguments: list[str]) -> tuple[str, object]:
        try:
            created = self._client.api.exec_create(
                container_id,
                [HOST, *arguments],
                stdin=True,
                stdout=True,
                stderr=True,
                privileged=False,
                user=USER,
                workdir=str(PurePosixPath("/") / "tmp"),
            )
        except DockerException as exc:
            raise BrowserError("browser-lost") from exc
        try:
            raw = self._client.api.exec_start(created["Id"], socket=True)
        except DockerException as exc:
            raise ExecStartError(created["Id"]) from exc
        sock = getattr(raw, "_sock", None)
        if sock is None:
            raise ExecStartError(created["Id"])
        return created["Id"], sock
