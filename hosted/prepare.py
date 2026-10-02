"""Hosted attachment preparation helpers (ADR-0093).

A Hosted helper runs under gVisor beside the Team's other hard-limited resources: its memory is reserved against the
global and Owner budgets for its whole life, it is labeled for capacity inventory, and Team teardown removes it.
"""

from __future__ import annotations

import secrets
from collections.abc import Callable, Iterator
from contextlib import contextmanager

import docker
import docker.errors

from hosted import container as container_spec
from hosted import state as runtime_state
from hosted.team import resources as hosted_resources
from prepare import helper as preparation_helper
from prepare import limits
from prepare import service as preparation

RUNTIME_LABEL = "team.prepare.runtime"
# Each helper's own capacity key, so a residual helper is never hidden behind a later helper's reservation.
KEY_LABEL = "team.prepare.key"


def helper_kwargs(client: object, *, team_id: str, owner: str, key: str) -> dict[str, object]:
    """The Hosted helper envelope: the controller's exact image under gVisor with Team accounting labels."""
    image = preparation_helper.own_image_id(client, (docker.errors.DockerException,))
    name = f"team-prepare-{team_id}-{key}"
    labels = {RUNTIME_LABEL: "1", KEY_LABEL: key, "team.id": team_id, "team.owner": owner}
    kwargs = preparation_helper.base_kwargs(image, name, labels)
    kwargs.update(
        runtime=container_spec.RUNTIME,
        security_opt=["no-new-privileges:true", "apparmor=docker-default"],
        ulimits=[docker.types.Ulimit(name="nofile", soft=64, hard=64)],
        log_config=container_spec.TEAM_LOG_CONFIG,
    )
    return kwargs


@contextmanager
def helper(
    team_id: str,
    owner: str,
    *,
    started: Callable[[object], None] = lambda _container: None,
    stopped: Callable[[object], None] = lambda _container: None,
) -> Iterator[preparation_helper.PreparationHelper]:
    """One helper for one preparation segment, its memory reserved under its own key until it is removed.

    A helper whose removal fails keeps its labels, so inventory accounts for it once its reservation ends, and no new
    helper of the Team starts until it is gone.
    """
    client = runtime_state._docker
    key = secrets.token_hex(8)

    def clear_residue() -> None:
        if not remove_helpers(team_id):
            raise preparation_helper.HelperUnavailableError("an earlier preparation helper remains")

    with (
        hosted_resources._reserve_capacity(
            f"prepare:{team_id}:{key}", owner, limits.HELPER_MEMORY_BYTES, team_slot=False
        ),
        preparation_helper.PreparationHelper(
            client,
            lambda: helper_kwargs(client, team_id=team_id, owner=owner, key=key),
            transport_errors=(docker.errors.DockerException,),
            started=started,
            stopped=stopped,
            clear_residue=clear_residue,
        ) as session,
    ):
        yield session


def prepare_attachments(
    files: list[preparation.StoredFile],
    *,
    team_id: str,
    owner: str,
    started: Callable[[object], None] = lambda _container: None,
    stopped: Callable[[object], None] = lambda _container: None,
) -> tuple[preparation.Attachment, ...]:
    """Prepare one message's files, holding a reserved helper only while images or PDFs need it."""
    return preparation.prepare_attachments(files, lambda: helper(team_id, owner, started=started, stopped=stopped))


def remove_helpers(team_id: str) -> bool:
    """Remove every preparation helper of one Team; already-absent is success, and a failure is retried."""
    try:
        containers = runtime_state._docker.containers.list(
            all=True, filters={"label": [RUNTIME_LABEL, f"team.id={team_id}"]}
        )
    except docker.errors.DockerException:
        return False
    for container in containers:
        try:
            container.remove(force=True)
        except docker.errors.NotFound:
            continue
        except docker.errors.DockerException:
            return False
    return True
