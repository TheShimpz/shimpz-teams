"""Local attachment preparation helpers (ADR-0093).

Local serializes preparation across its whole controller, which has only 256 MiB, so at most one helper exists at a
time. Each helper is labeled with the Space, Team, and `prepare` kind and is removed after its segment, on Stop,
with its Team, and by Space reset.
"""

from __future__ import annotations

import secrets
import threading
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass

from docker.errors import DockerException
from docker.types import LogConfig, Ulimit

from local.assistant.egress import PROFILE
from local.labels import KIND_LABEL, MANAGED_LABEL, PROFILE_LABEL, SPACE_LABEL, TEAM_LABEL
from prepare import helper as preparation_helper
from prepare import service as preparation

KIND = "prepare"
_SERIAL = threading.Lock()
# How often a turn waiting for the controller-wide admission notices that it was stopped.
_ADMISSION_POLL_SECONDS = 0.1


@dataclass(frozen=True, slots=True)
class TurnStop:
    """How Stop reaches one turn's preparation.

    ``started`` and ``stopped`` let a running helper occupy the turn's one active container slot; ``interrupt`` raises
    once the turn is stopped, so it leaves the wait for admission and reads no further file.
    """

    started: Callable[[object], None] = lambda _container: None
    stopped: Callable[[object], None] = lambda _container: None
    interrupt: Callable[[], None] = lambda: None


_UNSTOPPED = TurnStop()


def helper_labels(space_id: str, team_id: str) -> dict[str, str]:
    """The exact ownership labels of one Local preparation helper."""
    return {
        MANAGED_LABEL: "1",
        PROFILE_LABEL: PROFILE,
        SPACE_LABEL: space_id,
        KIND_LABEL: KIND,
        TEAM_LABEL: team_id,
    }


def helper_kwargs(client: object, *, space_id: str, team_id: str, cpuset_cpus: str | None) -> dict[str, object]:
    """The Local helper envelope: the controller's exact image, its CPU set, and no Docker log file."""
    image = preparation_helper.own_image_id(client, (DockerException,))
    name = f"shimpz-prepare-{team_id}-{secrets.token_hex(4)}"
    kwargs = preparation_helper.base_kwargs(image, name, helper_labels(space_id, team_id))
    kwargs.update(
        cpuset_cpus=cpuset_cpus,
        ulimits=[Ulimit(name="nofile", soft=64, hard=64)],
        log_config=LogConfig(type=LogConfig.types.NONE),
    )
    return kwargs


@contextmanager
def helper(
    client: object,
    *,
    space_id: str,
    team_id: str,
    cpuset_cpus: str | None,
    started: Callable[[object], None] = lambda _container: None,
    stopped: Callable[[object], None] = lambda _container: None,
) -> Iterator[preparation_helper.PreparationHelper]:
    """One helper for one preparation segment, removed before the segment continues."""

    def clear_residue() -> None:
        try:
            remove_helpers(client, space_id, team_id)
        except DockerException as exc:
            raise preparation_helper.HelperUnavailableError("an earlier preparation helper remains") from exc

    with preparation_helper.PreparationHelper(
        client,
        lambda: helper_kwargs(client, space_id=space_id, team_id=team_id, cpuset_cpus=cpuset_cpus),
        transport_errors=(DockerException,),
        started=started,
        stopped=stopped,
        clear_residue=clear_residue,
    ) as session:
        yield session


@contextmanager
def _admission(interrupt: Callable[[], None]) -> Iterator[None]:
    """Wait for the controller-wide admission, leaving the wait as soon as ``interrupt`` raises."""
    while not _SERIAL.acquire(timeout=_ADMISSION_POLL_SECONDS):
        interrupt()
    try:
        yield
    finally:
        _SERIAL.release()


def prepare_attachments(
    client: object,
    files: list[preparation.StoredFile],
    *,
    space_id: str,
    team_id: str,
    cpuset_cpus: str | None,
    stop: TurnStop = _UNSTOPPED,
) -> tuple[preparation.Attachment, ...]:
    """Prepare one message's files under the controller-wide preparation admission.

    Local's 256 MiB controller holds at most one preparation's originals, text, and helper at a time, from before the
    first original is read (ADR-0093). A turn stopped while it waits for that admission leaves at once.
    """
    return preparation.prepare_attachments(
        files,
        lambda: helper(
            client,
            space_id=space_id,
            team_id=team_id,
            cpuset_cpus=cpuset_cpus,
            started=stop.started,
            stopped=stop.stopped,
        ),
        _admission(stop.interrupt),
        stop.interrupt,
    )


def remove_helpers(client: object, space_id: str, team_id: str | None = None) -> int:
    """Remove every Local preparation helper of one Team, or of the whole Space; already-absent is success."""
    filters = [f"{MANAGED_LABEL}=1", f"{PROFILE_LABEL}={PROFILE}", f"{SPACE_LABEL}={space_id}", f"{KIND_LABEL}={KIND}"]
    if team_id is not None:
        filters.append(f"{TEAM_LABEL}={team_id}")
    removed = 0
    for container in client.containers.list(all=True, filters={"label": filters}):
        try:
            container.remove(force=True)
        except DockerException as exc:
            if getattr(getattr(exc, "response", None), "status_code", None) != 404:
                raise
        removed += 1
    return removed
