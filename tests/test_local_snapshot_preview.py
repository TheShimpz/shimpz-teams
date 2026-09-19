"""Bounded ephemeral Local Assistant preview reuse."""

from __future__ import annotations

import unittest
from unittest import mock

from docker.errors import DockerException

from local.install import preview, snapshots

IMAGE_ID = "sha256:" + ("a" * 64)
ICON = b"validated icon"


class LocalSnapshotPreviewCacheTests(unittest.TestCase):
    def setUp(self) -> None:
        self.client = mock.Mock()
        self.cache = preview.LocalSnapshotPreviewCache(self.client, "linux/amd64")

    def test_reuses_validated_bytes_after_revalidating_exact_presence(self) -> None:
        with (
            mock.patch.object(preview.snapshots, "preview_icon", return_value=ICON) as load,
            mock.patch.object(preview.snapshots, "require_candidate") as require,
        ):
            self.assertEqual(self.cache.icon(IMAGE_ID), ICON)
            self.assertEqual(self.cache.icon(IMAGE_ID), ICON)

        load.assert_called_once_with(self.client, IMAGE_ID, platform="linux/amd64")
        require.assert_called_once_with(self.client, IMAGE_ID, platform="linux/amd64")

    def test_removed_image_is_evicted_and_never_served(self) -> None:
        absent = snapshots.LocalSnapshotAbsentError("absent")
        with (
            mock.patch.object(preview.snapshots, "preview_icon", side_effect=(ICON, absent)) as load,
            mock.patch.object(preview.snapshots, "require_candidate", side_effect=absent),
        ):
            self.assertEqual(self.cache.icon(IMAGE_ID), ICON)
            with self.assertRaises(snapshots.LocalSnapshotAbsentError):
                self.cache.icon(IMAGE_ID)
            with self.assertRaises(snapshots.LocalSnapshotAbsentError):
                self.cache.icon(IMAGE_ID)

        self.assertEqual(load.call_count, 2)

    def test_transient_docker_failure_does_not_evict_validated_bytes(self) -> None:
        transient = snapshots.LocalSnapshotUnavailableError("offline")
        with (
            mock.patch.object(preview.snapshots, "preview_icon", return_value=ICON) as load,
            mock.patch.object(preview.snapshots, "require_candidate", side_effect=(transient, mock.DEFAULT)),
        ):
            self.assertEqual(self.cache.icon(IMAGE_ID), ICON)
            with self.assertRaises(snapshots.LocalSnapshotUnavailableError):
                self.cache.icon(IMAGE_ID)
            self.assertEqual(self.cache.icon(IMAGE_ID), ICON)

        load.assert_called_once_with(self.client, IMAGE_ID, platform="linux/amd64")

    def test_failed_preview_is_not_cached(self) -> None:
        failure = snapshots.LocalSnapshotError("invalid")
        with mock.patch.object(preview.snapshots, "preview_icon", side_effect=failure) as load:
            for _ in range(2):
                with self.assertRaises(snapshots.LocalSnapshotError):
                    self.cache.icon(IMAGE_ID)

        self.assertEqual(load.call_count, 2)

    def test_evicts_the_least_recently_used_icon_at_the_byte_bound(self) -> None:
        image_ids = [f"sha256:{value:064x}" for value in range(preview.MAX_CACHED_ICONS + 1)]
        large_icon = b"x" * (1024 * 1024)
        with (
            mock.patch.object(
                preview.snapshots,
                "preview_icon",
                return_value=large_icon,
            ) as load,
            mock.patch.object(preview.snapshots, "require_candidate"),
        ):
            for image_id in image_ids[:8]:
                self.cache.icon(image_id)
            self.cache.icon(image_ids[0])
            self.cache.icon(image_ids[8])
            self.cache.icon(image_ids[1])

        self.assertEqual(load.call_count, 10)

    def test_bounds_small_icons_by_the_staged_candidate_limit(self) -> None:
        image_ids = [f"sha256:{value:064x}" for value in range(preview.MAX_CACHED_ICONS + 1)]
        with (
            mock.patch.object(preview.snapshots, "preview_icon", return_value=b"x") as load,
            mock.patch.object(preview.snapshots, "require_candidate"),
        ):
            for image_id in image_ids:
                self.cache.icon(image_id)
            self.cache.icon(image_ids[0])

        self.assertEqual(load.call_count, preview.MAX_CACHED_ICONS + 2)

    def test_rejects_a_busy_miss_without_blocking(self) -> None:
        with (
            mock.patch.object(self.cache._miss_slots, "acquire", return_value=False) as acquire,
            self.assertRaises(preview.PreviewBusyError),
        ):
            self.cache.icon(IMAGE_ID)

        acquire.assert_called_once_with(blocking=False)

    def test_exact_presence_distinguishes_absence_from_daemon_failure(self) -> None:
        image_not_found = snapshots.ImageNotFound("missing")
        docker_failure = DockerException("offline")
        for failure, expected in (
            (image_not_found, snapshots.LocalSnapshotAbsentError),
            (docker_failure, snapshots.LocalSnapshotUnavailableError),
        ):
            with (
                self.subTest(expected=expected.__name__),
                mock.patch.object(self.client.images, "get", side_effect=failure),
                self.assertRaises(expected),
            ):
                snapshots.require_candidate(self.client, IMAGE_ID, platform="linux/amd64")


if __name__ == "__main__":
    unittest.main()
