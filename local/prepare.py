"""Local attachment preparation helpers (ADR-0093).

Local serializes preparation across its whole controller, which has only 256 MiB, so at most one helper exists at a
time. Each helper is labeled with the Space, Team, and `prepare` kind and is removed after its segment, on Stop,
with its Team, and by Space reset. A file preview prepares through the same serialized slot, admitted within a bounded
queue and deadline, and the Team's teardown and the Space reset end every preview of it first.
"""

import secrets
import threading
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field

from docker.types import LogConfig, Ulimit

from local import labels as local_labels
from local.errors import DOCKER_ERRORS
from prepare import helper as preparation_helper
from prepare import service as preparation

KIND = "prepare"
_SERIAL = threading.Lock()
# A file preview waits for the serialized slot at most this long, and at most this many wait; beyond either it is busy.
PREVIEW_QUEUE = 4
PREVIEW_DEADLINE_SECONDS = 10.0
# How long a teardown or reset waits for the previews it ended to leave.
PREVIEW_DRAIN_SECONDS = 30.0
_PREVIEW_ADMISSION = preparation.BoundedAdmission(_SERIAL, queue=PREVIEW_QUEUE, deadline=PREVIEW_DEADLINE_SECONDS)
_SPACE = "*"


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
    return local_labels.ownership_labels(space_id, KIND, team_id)


def helper_kwargs(client: object, *, space_id: str, team_id: str, cpuset_cpus: str | None) -> dict[str, object]:
    """The Local helper envelope: the controller's exact image, its CPU set, and no Docker log file."""
    image = preparation_helper.own_image_id(client, DOCKER_ERRORS)
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
        except DOCKER_ERRORS as exc:
            raise preparation_helper.HelperUnavailableError("an earlier preparation helper remains") from exc

    with preparation_helper.PreparationHelper(
        client,
        lambda: helper_kwargs(client, space_id=space_id, team_id=team_id, cpuset_cpus=cpuset_cpus),
        transport_errors=DOCKER_ERRORS,
        started=started,
        stopped=stopped,
        clear_residue=clear_residue,
    ) as session:
        yield session


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
        preparation.admission(_SERIAL, stop.interrupt),
        stop.interrupt,
    )


class PreviewEndedError(RuntimeError):
    """The preview was ended by its Team's teardown or the Space reset."""


class PreviewDrainError(RuntimeError):
    """A preview that was ended did not leave in time, or its helper could not be removed."""


@dataclass(eq=False, slots=True)
class _Preview:
    team_id: str
    book: PreviewBook
    ended: bool = False
    container: object | None = None

    def started(self, container: object) -> None:
        # A helper that starts once its preview was ended is refused here and removed when its segment closes.
        with self.book.guard:
            if self.ended:
                raise PreviewEndedError("the preview was ended")
            self.container = container

    def stopped(self, container: object) -> None:
        with self.book.guard:
            if self.container is container:
                self.container = None

    def interrupt(self) -> None:
        with self.book.guard:
            if self.ended:
                raise PreviewEndedError("the preview was ended")

    @property
    def stop(self) -> TurnStop:
        return TurnStop(started=self.started, stopped=self.stopped, interrupt=self.interrupt)


@dataclass(slots=True)
class PreviewBook:
    """Every file preview and download under way, each bound to its Team for as long as it runs.

    A Team's teardown and the Space reset each close it for their whole duration: no preview or download of it
    opens, every queued or running one is ended, a running preview's helper is removed, and they wait
    until every ended one left before they continue, so none can start a helper or send a file after they finished. A
    download whose last check passed before the close finishes sending first; its check fails after it.
    """

    guard: threading.Condition = field(default_factory=threading.Condition)
    _previews: set[_Preview] = field(default_factory=set)
    _closed: dict[str, int] = field(default_factory=dict)

    @contextmanager
    def open(self, team_id: str) -> Iterator[_Preview]:
        with self.guard:
            if self._closed.get(team_id) or self._closed.get(_SPACE):
                raise PreviewEndedError("the Team's previews are closed")
            preview = _Preview(team_id, self)
            self._previews.add(preview)
        try:
            yield preview
        finally:
            with self.guard:
                self._previews.discard(preview)
                self.guard.notify_all()

    @contextmanager
    def closed(self, team_id: str | None = None) -> Iterator[None]:
        """Close one Team's previews, or the whole Space's, for the duration, after ending the ones under way."""
        key = _SPACE if team_id is None else team_id
        with self.guard:
            self._closed[key] = self._closed.get(key, 0) + 1
            ending = [preview for preview in self._previews if team_id is None or preview.team_id == team_id]
            for preview in ending:
                preview.ended = True
            containers = [preview.container for preview in ending if preview.container is not None]
        try:
            self._drain(ending, containers)
            yield
        finally:
            with self.guard:
                self._closed[key] -= 1
                if not self._closed[key]:
                    del self._closed[key]

    def _drain(self, ending: list[_Preview], containers: list[object]) -> None:
        for container in containers:
            try:
                container.remove(force=True)
            except DOCKER_ERRORS as exc:
                if getattr(getattr(exc, "response", None), "status_code", None) != 404:
                    raise PreviewDrainError("a preview helper could not be removed") from exc
        with self.guard:
            if not self.guard.wait_for(
                lambda: not any(preview in self._previews for preview in ending), PREVIEW_DRAIN_SECONDS
            ):
                raise PreviewDrainError("a preview did not end in time")


# The controller's one book of previews, as its preparation slot is one: one controller runs per process.
PREVIEWS = PreviewBook()


def prepare_rendition(
    client: object,
    item: preparation.StoredFile,
    *,
    space_id: str,
    team_id: str,
    cpuset_cpus: str | None,
    stop: TurnStop,
    page: int | None = None,
) -> preparation.Rendition | None:
    """One stored file's, or one PDF page's, sanitized rendition through the preparation slot, within its queue."""
    return preparation.prepare_rendition(
        item,
        lambda: helper(
            client,
            space_id=space_id,
            team_id=team_id,
            cpuset_cpus=cpuset_cpus,
            started=stop.started,
            stopped=stop.stopped,
        ),
        _PREVIEW_ADMISSION.admit(stop.interrupt),
        stop.interrupt,
        page,
    )


def remove_helpers(client: object, space_id: str, team_id: str | None = None) -> None:
    """Remove every Local preparation helper of one Team, or of the whole Space; already-absent is success."""
    filters = local_labels.label_filter(local_labels.ownership_labels(space_id, KIND, team_id))
    for container in client.containers.list(all=True, filters={"label": filters}):
        try:
            container.remove(force=True)
        except DOCKER_ERRORS as exc:
            if getattr(getattr(exc, "response", None), "status_code", None) != 404:
                raise
