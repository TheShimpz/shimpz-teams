"""Real-Docker proof that restaging moves the current Local snapshot tag and collection keeps what Teams use."""

import os
import sys
import tempfile
import unittest
from contextlib import suppress
from pathlib import Path
from types import SimpleNamespace

import docker
from docker.errors import APIError, ImageNotFound

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from local.install import collector, snapshots


class LocalSnapshotCollectionDockerTests(unittest.TestCase):
    @unittest.skipUnless(os.environ.get("SHIMPZ_RUN_DOCKER_TESTS") == "1", "real Docker test is opt-in")
    def test_restaging_moves_the_current_tag_and_collects_only_the_released_superseded_image(self) -> None:
        client = docker.from_env()
        assistant_id = f"collector-proof-{os.getpid()}"
        reference = snapshots.canonical_reference(assistant_id)
        image_ids: list[str] = []

        def build(nonce: str) -> str:
            with tempfile.TemporaryDirectory() as build_root:
                Path(build_root, "Dockerfile").write_text(
                    "FROM scratch\n"
                    f"LABEL {snapshots.LOCAL_STAGE_LABEL}={snapshots.LOCAL_STAGE_VALUE} "
                    f"{snapshots.ASSISTANT_LABEL}={assistant_id} proof.nonce={nonce}\n",
                    encoding="utf-8",
                )
                image, _logs = client.images.build(path=build_root, tag=reference, rm=True, forcerm=True)
            image_ids.append(image.id)
            return image.id

        try:
            first = build("first")
            second = build("second")
            third = build("third")
            self.assertEqual(client.api.inspect_image(first)["RepoTags"], [])
            self.assertEqual(client.api.inspect_image(third)["RepoTags"], [reference])
            discovered = {
                summary["Id"]
                for summary in client.api.images(
                    filters={"reference": [f"{snapshots.LOCAL_SNAPSHOT_REPOSITORY}/*:{snapshots.LOCAL_SNAPSHOT_TAG}"]}
                )
            }
            self.assertIn(third, discovered)
            self.assertFalse({first, second} & discovered)

            # The collector sees only this test's images, so it can never touch other daemon inventory.
            real_images = client.api.images

            def own_images(**kwargs):
                return [summary for summary in real_images(**kwargs) if summary["Id"] in image_ids]

            scoped = SimpleNamespace(
                api=SimpleNamespace(
                    images=own_images,
                    containers=client.api.containers,
                    inspect_image=client.api.inspect_image,
                ),
                images=client.images,
            )
            registry = SimpleNamespace(images=lambda: (second,))
            subject = collector.SupersededSnapshotCollector(scoped, registry, grace_seconds=0)
            subject.collect()
            subject.collect()

            with self.assertRaises(ImageNotFound):
                client.images.get(first)
            self.assertEqual(client.images.get(second).id, second)
            self.assertEqual(client.api.inspect_image(third)["RepoTags"], [reference])

            # Unstage's no-force removal of the current snapshot is refused while a container references it,
            # and the refusal leaves the Assistant's tag in place.
            holder = client.containers.create(third, command=["/hold"])
            try:
                with self.assertRaises(APIError):
                    client.images.remove(image=third, force=False, noprune=True)
                self.assertEqual(client.api.inspect_image(third)["RepoTags"], [reference])
            finally:
                holder.remove(force=True)
        finally:
            for image_id in reversed(image_ids):
                with suppress(ImageNotFound):
                    client.images.remove(image=image_id, force=True, noprune=True)


if __name__ == "__main__":
    unittest.main()
