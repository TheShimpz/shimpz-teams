"""Eventual removal of superseded Local Assistant snapshots that no Team binds or runs."""

import logging
import time
from collections.abc import Callable

from docker.errors import DockerException, ImageNotFound

from install import bindings
from local.install import snapshots

# An install admits only an Assistant's current (tagged) snapshot, so once an image has been seen superseded,
# unbound, and unused for this long, any install that admitted it has long since committed its binding or failed.
GRACE_SECONDS = 15 * 60
_UNTAGGED = (None, [], ["<none>:<none>"])
_UNDIGESTED = (None, [], ["<none>@<none>"])
log = logging.getLogger("shimpz-team-local-snapshot-collector")


def _local_digests(labels: dict, repo_digests: object, image_id: str) -> bool:
    """A superseded snapshot was never pulled: no digest, or its own digest on a containerd image store."""
    if repo_digests == []:
        return True
    assistant_id = labels.get(snapshots.ASSISTANT_LABEL)
    return isinstance(assistant_id, str) and snapshots.local_repo_digests_valid(repo_digests, assistant_id, image_id)


class SupersededSnapshotCollector:
    """Delete stage-labeled images that lost their tag once no binding or container keeps them."""

    def __init__(
        self,
        client,
        registry,
        *,
        clock: Callable[[], float] = time.monotonic,
        grace_seconds: float = GRACE_SECONDS,
    ) -> None:
        self._client = client
        self._registry = registry
        self._clock = clock
        self._grace_seconds = grace_seconds
        self._observed: dict[str, float] = {}

    def collect(self) -> None:
        superseded = self._superseded()
        if superseded is None:
            return
        self._observed = {image_id: seen for image_id, seen in self._observed.items() if image_id in superseded}
        if not superseded:
            return
        retained = self._retained()
        if retained is None:
            return
        now = self._clock()
        for image_id in sorted(superseded):
            if image_id in retained:
                self._observed.pop(image_id, None)
                continue
            first_seen = self._observed.setdefault(image_id, now)
            if now - first_seen >= self._grace_seconds and self._remove(image_id):
                self._observed.pop(image_id, None)

    def _superseded(self) -> set[str] | None:
        # Only an untagged image can be superseded, and Docker lists those from its name index.
        try:
            summaries = self._client.api.images(filters={"dangling": ["true"]})
        except DockerException:
            log.warning("Superseded snapshot collection deferred: Docker inventory is unavailable")
            return None
        if not isinstance(summaries, list):
            log.warning("Superseded snapshot collection deferred: Docker inventory is invalid")
            return None
        return {
            summary["Id"]
            for summary in summaries
            if isinstance(summary, dict)
            and isinstance(summary.get("Id"), str)
            and isinstance(summary.get("Labels"), dict)
            and summary["Labels"].get(snapshots.LOCAL_STAGE_LABEL) == snapshots.LOCAL_STAGE_VALUE
            and summary.get("RepoTags") in _UNTAGGED
            and (
                summary.get("RepoDigests") in _UNDIGESTED
                or _local_digests(summary["Labels"], summary.get("RepoDigests"), summary["Id"])
            )
        }

    def _retained(self) -> set[str] | None:
        """Return every image a binding of any Team or any container in any state still references."""
        retained: set[str] = set()
        try:
            for image in self._registry.images():
                try:
                    retained.add(self._client.images.get(image).id)
                except ImageNotFound:
                    continue
            for container in self._client.api.containers(all=True):
                retained.add(container["ImageID"])
        except bindings.DynamicAssistantError, DockerException, KeyError, TypeError:
            log.warning("Superseded snapshot collection deferred: bindings or containers are unavailable")
            return None
        return retained

    def _remove(self, image_id: str) -> bool:
        try:
            attrs = self._client.api.inspect_image(image_id)
        except ImageNotFound:
            return True
        except DockerException:
            log.warning("Superseded snapshot collection deferred: image metadata is unavailable")
            return False
        config = attrs.get("Config") if isinstance(attrs, dict) else None
        labels = config.get("Labels") if isinstance(config, dict) else None
        if (
            not isinstance(labels, dict)
            or labels.get(snapshots.LOCAL_STAGE_LABEL) != snapshots.LOCAL_STAGE_VALUE
            or attrs.get("RepoTags") != []
            or not _local_digests(labels, attrs.get("RepoDigests"), image_id)
        ):
            return True
        try:
            self._client.images.remove(image=image_id, force=False, noprune=True)
        except ImageNotFound:
            return True
        except DockerException:
            log.warning("Superseded snapshot collection deferred: Docker refused the removal")
            return False
        return True
