"""The networkless preparation helper: one short-lived container per segment, one fresh process per file.

The helper runs the controller's own exact image with a fixed idle entrypoint and executes the fixed worker module
once per file. It has no network, capabilities, credentials, Docker socket, or Team storage mount, and every answer
is bounded and re-validated by the controller. A file whose process times out, exits non-zero, or writes to standard
error is refused, and the helper is replaced before the next file. The caller removes the helper before inference or
any human wait.
"""

import socket
from collections.abc import Callable
from contextlib import suppress
from pathlib import PurePosixPath

from action import execution as action_execution
from prepare import limits, worker

HELPER_PYTHON = str(PurePosixPath("/") / "opt" / "venv" / "bin" / "python")
IDLE_ENTRYPOINT = [HELPER_PYTHON, "-c", "import signal; signal.pause()"]
WORKER_ARGV = [HELPER_PYTHON, "-m", "prepare.worker"]
HELPER_USER = "65534:65534"
HELPER_WORKDIR = str(PurePosixPath("/") / "app")
HELPER_TMPFS = {str(PurePosixPath("/") / "tmp"): "size=16m,mode=1777,noexec,nosuid,nodev"}
_UNREADABLE = {"type": "opaque", "reason": "unreadable"}


class HelperUnavailableError(RuntimeError):
    """The preparation helper could not be started or removed."""


def own_image_id(client: object, transport_errors: tuple[type[BaseException], ...]) -> str:
    """Return the exact image id of the running controller container, so the helper runs the same image."""
    try:
        image_id = client.containers.get(socket.gethostname()).image.id
    except (*transport_errors, AttributeError) as exc:
        raise HelperUnavailableError("the controller image is unknown") from exc
    if not isinstance(image_id, str) or not image_id.startswith("sha256:"):
        raise HelperUnavailableError("the controller image is unknown")
    return image_id


def base_kwargs(image: str, name: str, labels: dict[str, str]) -> dict[str, object]:
    """The profile-neutral helper envelope; a profile adds only its runtime, CPU set, and accounting labels."""
    return {
        "image": image,
        "name": name,
        "entrypoint": list(IDLE_ENTRYPOINT),
        "command": [],
        "user": HELPER_USER,
        "working_dir": HELPER_WORKDIR,
        "environment": {"PYTHONDONTWRITEBYTECODE": "1"},
        "network_mode": "none",
        "read_only": True,
        "cap_drop": ["ALL"],
        "security_opt": ["no-new-privileges:true"],
        "privileged": False,
        "ipc_mode": "private",
        "cgroupns": "private",
        "mounts": [],
        "volumes": {},
        "tmpfs": dict(HELPER_TMPFS),
        "mem_limit": limits.HELPER_MEMORY_BYTES,
        "memswap_limit": limits.HELPER_MEMORY_BYTES,
        "nano_cpus": limits.HELPER_NANO_CPUS,
        "pids_limit": limits.HELPER_PIDS,
        "restart_policy": {"Name": "no"},
        "labels": dict(labels),
        "detach": True,
    }


class PreparationHelper:
    """One helper container for one preparation segment."""

    def __init__(
        self,
        client: object,
        kwargs: Callable[[], dict[str, object]],
        *,
        transport_errors: tuple[type[BaseException], ...],
        started: Callable[[object], None] = lambda _container: None,
        stopped: Callable[[object], None] = lambda _container: None,
        clear_residue: Callable[[], None] = lambda: None,
    ) -> None:
        self._client = client
        self._kwargs = kwargs
        self._transport_errors = transport_errors
        self._started = started
        self._stopped = stopped
        # Removes every earlier helper of the same owner before a new one may start, raising when one remains.
        self._clear_residue = clear_residue
        self._container: object | None = None

    def __enter__(self) -> PreparationHelper:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    def prepare(self, kind: str, data: bytes) -> dict[str, object]:
        """Run one fresh worker process for one file and return its unvalidated answer."""
        container = self._running()
        try:
            answer = action_execution.rpc_exchange(
                container.id,
                list(WORKER_ARGV),
                worker.encode_request(kind, data),
                action_execution.RpcExchangeStrategy(
                    api=self._client.api,
                    user=HELPER_USER,
                    workdir=HELPER_WORKDIR,
                    timeout=limits.HELPER_WALL_SECONDS,
                    maximum=limits.MAX_HELPER_OUTPUT_BYTES,
                    transport_errors=self._transport_errors,
                    fail_stop=self.close,
                    cancelled=lambda _exc: None,
                    close_stream=_close_stream,
                ),
            )
        except action_execution.RpcExchangeError:
            return dict(_UNREADABLE)
        return answer if isinstance(answer, dict) else dict(_UNREADABLE)

    def close(self) -> None:
        """Remove the helper container, keeping ownership of it until its removal or absence is proved.

        A failed removal raises and leaves the helper owned: it is not reported stopped, a retry removes it again, and
        no replacement helper starts while it remains.
        """
        container = self._container
        if container is None:
            return
        try:
            container.remove(force=True)
        except self._transport_errors as exc:
            if not _absent(exc):
                raise HelperUnavailableError("the preparation helper could not be removed") from exc
        self._container = None
        self._stopped(container)

    def _running(self) -> object:
        if self._container is not None:
            return self._container
        self._clear_residue()
        try:
            container = self._client.containers.create(**self._kwargs())
        except self._transport_errors as exc:
            raise HelperUnavailableError("the preparation helper could not be started") from exc
        self._container = container
        try:
            self._started(container)
            container.start()
        except self._transport_errors as exc:
            with suppress(HelperUnavailableError):
                self.close()
            raise HelperUnavailableError("the preparation helper could not be started") from exc
        return container


def _absent(exc: BaseException) -> bool:
    return getattr(getattr(exc, "response", None), "status_code", None) == 404


def _close_stream(stream: object) -> None:
    with suppress(OSError, AttributeError):
        action_execution.close_exec_stream(stream)
