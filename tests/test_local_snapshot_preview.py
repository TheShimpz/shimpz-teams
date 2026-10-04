"""Bounded ephemeral Local Assistant preview reuse."""

from __future__ import annotations

import threading
import unittest
from concurrent.futures import Future
from unittest import mock

from docker.errors import DockerException

from local.install import preview, snapshots

IMAGE_ID = "sha256:" + ("a" * 64)
ICON = b"validated icon"
SUMMARIES = {"en": "Summary.", "pt": "Resumo."}


def _preview(icon: bytes = ICON) -> snapshots.SnapshotPreview:
    return snapshots.SnapshotPreview(icon=icon, summaries=SUMMARIES)


class LocalSnapshotPreviewCacheTests(unittest.TestCase):
    def setUp(self) -> None:
        self.client = mock.Mock()
        self.cache = preview.LocalSnapshotPreviewCache(self.client, "linux/amd64")

    def test_reuses_validated_bytes_after_revalidating_exact_presence(self) -> None:
        with (
            mock.patch.object(preview.snapshots, "preview", return_value=_preview()) as load,
            mock.patch.object(preview.snapshots, "require_candidate") as require,
        ):
            self.assertEqual(self.cache.icon(IMAGE_ID), ICON)
            self.assertEqual(self.cache.icon(IMAGE_ID), ICON)

        load.assert_called_once_with(self.client, IMAGE_ID, platform="linux/amd64")
        require.assert_called_once_with(self.client, IMAGE_ID, platform="linux/amd64")

    def test_removed_image_is_evicted_and_never_served(self) -> None:
        absent = snapshots.LocalSnapshotAbsentError("absent")
        with (
            mock.patch.object(preview.snapshots, "preview", side_effect=(_preview(), absent)) as load,
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
            mock.patch.object(preview.snapshots, "preview", return_value=_preview()) as load,
            mock.patch.object(preview.snapshots, "require_candidate", side_effect=(transient, mock.DEFAULT)),
        ):
            self.assertEqual(self.cache.icon(IMAGE_ID), ICON)
            with self.assertRaises(snapshots.LocalSnapshotUnavailableError):
                self.cache.icon(IMAGE_ID)
            self.assertEqual(self.cache.icon(IMAGE_ID), ICON)

        load.assert_called_once_with(self.client, IMAGE_ID, platform="linux/amd64")

    def test_failed_preview_is_not_cached(self) -> None:
        failure = snapshots.LocalSnapshotError("invalid")
        with mock.patch.object(preview.snapshots, "preview", side_effect=failure) as load:
            for _ in range(2):
                with self.assertRaises(snapshots.LocalSnapshotError):
                    self.cache.icon(IMAGE_ID)

        self.assertEqual(load.call_count, 2)

    def test_evicts_the_least_recently_used_icon_at_the_byte_bound(self) -> None:
        image_ids = [f"sha256:{value:064x}" for value in range(preview.MAX_CACHED_PREVIEWS + 1)]
        # Eight previews fill the byte bound exactly; a ninth evicts the least recently used one.
        summary_bytes = sum(len(text.encode()) for text in SUMMARIES.values())
        large_icon = b"x" * (1024 * 1024 - summary_bytes)
        with (
            mock.patch.object(
                preview.snapshots,
                "preview",
                return_value=_preview(large_icon),
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
        image_ids = [f"sha256:{value:064x}" for value in range(preview.MAX_CACHED_PREVIEWS + 1)]
        with (
            mock.patch.object(preview.snapshots, "preview", return_value=_preview(b"x")) as load,
            mock.patch.object(preview.snapshots, "require_candidate"),
        ):
            for image_id in image_ids:
                self.cache.icon(image_id)
            self.cache.icon(image_ids[0])

        self.assertEqual(load.call_count, preview.MAX_CACHED_PREVIEWS + 2)

    def test_serves_each_summary_from_the_same_validated_preview(self) -> None:
        with (
            mock.patch.object(preview.snapshots, "preview", return_value=_preview()) as load,
            mock.patch.object(preview.snapshots, "require_candidate") as require,
        ):
            self.assertEqual(self.cache.icon(IMAGE_ID), ICON)
            self.assertEqual(self.cache.summary(IMAGE_ID, "pt"), "Resumo.")
            self.assertEqual(self.cache.summary(IMAGE_ID, "en"), "Summary.")

        load.assert_called_once_with(self.client, IMAGE_ID, platform="linux/amd64")
        self.assertEqual(require.call_count, 2)

    def test_a_repeated_miss_replaces_its_entry_without_leaking_its_budget(self) -> None:
        with mock.patch.object(preview.snapshots, "preview", return_value=_preview()):
            self.cache._remember(IMAGE_ID, _preview())
            self.cache._remember(IMAGE_ID, _preview(b"other icon"))
        expected = len(b"other icon") + sum(len(text.encode()) for text in SUMMARIES.values())
        self.assertEqual(self.cache._cached_bytes, expected)
        self.cache._discard("sha256:" + ("b" * 64))
        self.cache._discard(IMAGE_ID)
        self.assertEqual(self.cache._cached_bytes, 0)

    def test_rejects_a_busy_miss_without_blocking(self) -> None:
        with (
            mock.patch.object(self.cache._miss_slots, "acquire", return_value=False) as acquire,
            self.assertRaises(preview.PreviewBusyError),
        ):
            self.cache.icon(IMAGE_ID)

        acquire.assert_called_once_with(blocking=False)

    def _join_one_extraction(self, outcome) -> tuple[list, mock.Mock]:
        """Request the icon, then the summary while the icon's extraction runs; return both outcomes."""
        started, joined = threading.Event(), threading.Event()

        class JoiningFuture(Future):
            def result(self, timeout=None):
                joined.set()
                return super().result(timeout)

        def extract(*_args, **_kwargs):
            started.set()
            # Wait until the summary request joins; a duplicate extraction would instead time out and be counted.
            joined.wait(5)
            if isinstance(outcome, BaseException):
                raise outcome
            return outcome

        outcomes: list = [None, None]

        def request(index, load) -> None:
            try:
                outcomes[index] = load()
            except snapshots.LocalSnapshotError as exc:
                outcomes[index] = exc

        with (
            mock.patch.object(preview, "Future", JoiningFuture, create=True),
            mock.patch.object(preview.snapshots, "preview", side_effect=extract) as load,
            mock.patch.object(preview.snapshots, "require_candidate"),
        ):
            icon = threading.Thread(target=request, args=(0, lambda: self.cache.icon(IMAGE_ID)))
            icon.start()
            self.assertTrue(started.wait(5))
            summary = threading.Thread(target=request, args=(1, lambda: self.cache.summary(IMAGE_ID, "pt")))
            summary.start()
            for thread in (icon, summary):
                thread.join(5)
        return outcomes, load

    def test_concurrent_icon_and_summary_requests_share_one_extraction(self) -> None:
        outcomes, load = self._join_one_extraction(_preview())

        self.assertEqual(outcomes, [ICON, "Resumo."])
        load.assert_called_once_with(self.client, IMAGE_ID, platform="linux/amd64")
        self.assertEqual(self.cache._extractions, {})
        # The shared extraction held one slot and returned it: every slot is free again.
        slots = [self.cache._miss_slots.acquire(blocking=False) for _ in range(preview.MAX_CONCURRENT_MISSES + 1)]
        self.assertEqual(slots, [True] * preview.MAX_CONCURRENT_MISSES + [False])

    def test_a_failed_shared_extraction_fails_every_joined_request_and_caches_nothing(self) -> None:
        failure = snapshots.LocalSnapshotError("invalid")
        outcomes, load = self._join_one_extraction(failure)

        self.assertEqual(outcomes, [failure, failure])
        load.assert_called_once_with(self.client, IMAGE_ID, platform="linux/amd64")
        self.assertEqual((self.cache._previews, self.cache._extractions), ({}, {}))

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
