import types
import unittest
from unittest import mock

from docker.errors import DockerException, ImageNotFound

from install import bindings
from local.install import collector, snapshots

SUPERSEDED = "sha256:" + ("a" * 64)
BOUND = "sha256:" + ("b" * 64)
RUNNING = "sha256:" + ("c" * 64)
STAGE = {snapshots.LOCAL_STAGE_LABEL: snapshots.LOCAL_STAGE_VALUE}


def _summary(image_id: str, **overrides) -> dict[str, object]:
    return {"Id": image_id, "Labels": dict(STAGE), "RepoTags": [], "RepoDigests": [], **overrides}


class _Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def _collector(summaries, *, bound=(), containers=()):
    client = mock.Mock()
    client.api.images.return_value = summaries
    client.api.containers.return_value = [{"ImageID": image_id} for image_id in containers]
    client.api.inspect_image.side_effect = lambda image_id: {
        "Config": {"Labels": dict(STAGE)},
        "RepoTags": [],
        "RepoDigests": [],
    }
    client.images.get.side_effect = lambda reference: types.SimpleNamespace(id=reference)
    registry = mock.Mock()
    registry.all.return_value = tuple(types.SimpleNamespace(image=image_id) for image_id in bound)
    clock = _Clock()
    return collector.SupersededSnapshotCollector(client, registry, clock=clock), client, registry, clock


class SupersededSnapshotCollectorTests(unittest.TestCase):
    def test_removes_an_unbound_unused_superseded_snapshot_only_after_the_grace_period(self) -> None:
        subject, client, _registry, clock = _collector([_summary(SUPERSEDED)])

        subject.collect()
        clock.now += collector.GRACE_SECONDS - 1
        subject.collect()
        client.images.remove.assert_not_called()

        clock.now += 1
        subject.collect()

        client.api.images.assert_called_with(filters={"dangling": ["true"]})
        client.images.remove.assert_called_once_with(image=SUPERSEDED, force=False, noprune=True)

    def test_keeps_images_a_binding_or_any_container_references_and_restarts_their_grace(self) -> None:
        subject, client, registry, clock = _collector(
            [_summary(SUPERSEDED), _summary(BOUND), _summary(RUNNING)],
            bound=(BOUND,),
            containers=(RUNNING,),
        )
        subject.collect()
        clock.now += collector.GRACE_SECONDS
        subject.collect()
        client.images.remove.assert_called_once_with(image=SUPERSEDED, force=False, noprune=True)

        # A stopped Team keeps its binding without a container; once released, the image waits a full grace.
        registry.all.return_value = ()
        client.api.containers.return_value = []
        subject.collect()
        clock.now += collector.GRACE_SECONDS - 1
        subject.collect()
        self.assertEqual(client.images.remove.call_count, 1)
        clock.now += 1
        subject.collect()
        removed = {call.kwargs["image"] for call in client.images.remove.call_args_list}
        self.assertEqual(removed, {SUPERSEDED, BOUND, RUNNING})

    def test_considers_only_untagged_current_stage_images(self) -> None:
        summaries = [
            _summary(SUPERSEDED, RepoTags=["shimpz-local/fixture:staged"]),
            _summary(BOUND, RepoDigests=["registry/image@sha256:" + ("d" * 64)]),
            _summary(RUNNING, Labels={snapshots.LOCAL_STAGE_LABEL: "assistant-v2"}),
            {"Id": "sha256:" + ("e" * 64), "Labels": None, "RepoTags": [], "RepoDigests": []},
            "not-a-summary",
        ]
        subject, client, registry, clock = _collector(summaries)
        subject.collect()
        clock.now += collector.GRACE_SECONDS
        subject.collect()
        client.images.remove.assert_not_called()
        registry.all.assert_not_called()

    def test_accepts_the_untagged_markers_older_daemons_report(self) -> None:
        subject, client, _registry, clock = _collector(
            [_summary(SUPERSEDED, RepoTags=["<none>:<none>"], RepoDigests=["<none>@<none>"])]
        )
        subject.collect()
        clock.now += collector.GRACE_SECONDS
        subject.collect()
        client.images.remove.assert_called_once()

    def test_reinspects_before_removal_and_never_deletes_an_image_that_gained_a_tag(self) -> None:
        subject, client, _registry, clock = _collector([_summary(SUPERSEDED)])
        client.api.inspect_image.side_effect = lambda image_id: {
            "Config": {"Labels": dict(STAGE)},
            "RepoTags": ["shimpz-local/fixture:staged"],
            "RepoDigests": [],
        }
        subject.collect()
        clock.now += collector.GRACE_SECONDS
        subject.collect()
        client.images.remove.assert_not_called()

    def test_malformed_image_metadata_never_deletes_or_stops_collection(self) -> None:
        for metadata in (["not", "a", "dict"], {"Config": ["labels"]}, {"Config": {"Labels": "stage"}}, "attrs"):
            with self.subTest(metadata=metadata):
                subject, client, _registry, clock = _collector([_summary(SUPERSEDED)])
                client.api.inspect_image.side_effect = lambda image_id, metadata=metadata: metadata
                subject.collect()
                clock.now += collector.GRACE_SECONDS
                subject.collect()
                client.images.remove.assert_not_called()

    def test_docker_and_registry_failures_defer_without_losing_the_observation(self) -> None:
        with self.assertLogs("shimpz-team-local-snapshot-collector", level="WARNING") as logs:
            subject, client, _registry, _clock = _collector([])
            client.api.images.side_effect = DockerException("offline")
            subject.collect()

            subject, client, _registry, _clock = _collector({"unexpected": True})
            subject.collect()

            subject, client, registry, _clock = _collector([_summary(SUPERSEDED)])
            registry.all.side_effect = bindings.DynamicAssistantError("unreadable")
            subject.collect()

            subject, client, _registry, _clock = _collector([_summary(SUPERSEDED)])
            client.api.containers.side_effect = DockerException("offline")
            subject.collect()

            subject, client, _registry, clock = _collector([_summary(SUPERSEDED)])
            subject.collect()
            clock.now += collector.GRACE_SECONDS
            client.api.inspect_image.side_effect = DockerException("offline")
            subject.collect()
            client.api.inspect_image.side_effect = lambda image_id: {
                "Config": {"Labels": dict(STAGE)},
                "RepoTags": [],
                "RepoDigests": [],
            }
            client.images.remove.side_effect = DockerException("image is in use")
            subject.collect()
            client.images.remove.side_effect = None
            subject.collect()
        self.assertEqual(len(logs.output), 6)
        self.assertEqual(client.images.remove.call_count, 2)

    def test_absent_images_count_as_collected_and_stale_observations_are_forgotten(self) -> None:
        subject, client, _registry, clock = _collector([_summary(SUPERSEDED)])
        subject.collect()
        client.api.images.return_value = []
        subject.collect()
        client.api.images.return_value = [_summary(SUPERSEDED)]
        clock.now += collector.GRACE_SECONDS
        subject.collect()
        client.images.remove.assert_not_called()

        clock.now += collector.GRACE_SECONDS
        client.api.inspect_image.side_effect = ImageNotFound("gone")
        subject.collect()
        client.images.remove.assert_not_called()

        client.api.inspect_image.side_effect = lambda image_id: {
            "Config": {"Labels": dict(STAGE)},
            "RepoTags": [],
            "RepoDigests": [],
        }
        subject.collect()
        clock.now += collector.GRACE_SECONDS
        client.images.remove.side_effect = ImageNotFound("gone")
        subject.collect()
        client.images.remove.assert_called_once()

    def test_a_missing_bound_image_does_not_block_collection(self) -> None:
        subject, client, _registry, clock = _collector([_summary(SUPERSEDED)], bound=(BOUND,))
        client.images.get.side_effect = ImageNotFound("gone")
        subject.collect()
        clock.now += collector.GRACE_SECONDS
        subject.collect()
        client.images.remove.assert_called_once_with(image=SUPERSEDED, force=False, noprune=True)


if __name__ == "__main__":
    unittest.main()
