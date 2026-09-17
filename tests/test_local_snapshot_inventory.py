"""Freshness and concurrency contracts for cached Local snapshot discovery."""

from __future__ import annotations

import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from unittest import mock

from docker.errors import DockerException

from local.install import inventory, snapshots

EMPTY: tuple[snapshots.LocalSnapshotCandidate, ...] = ()
CANDIDATE = snapshots.LocalSnapshotCandidate(
    "fixture-assistant",
    "0.1.0",
    "Fixture Assistant",
    "Exercise cached discovery.",
    ("@fixture",),
    ("ping",),
    (),
    "sha256:" + ("a" * 64),
    "linux/amd64",
    "2026-09-17T00:00:00Z",
)
CURRENT = (CANDIDATE,)


class LocalSnapshotInventoryTests(unittest.TestCase):
    def test_default_loader_forwards_the_validated_platform(self) -> None:
        client = mock.Mock()
        with mock.patch.object(inventory.snapshots, "list_candidates", return_value=CURRENT) as list_candidates:
            cache = inventory.LocalSnapshotInventory(client, "linux/amd64")

            self.assertEqual(cache.candidates(), CURRENT)

        list_candidates.assert_called_once_with(client, platform="linux/amd64")

    def test_warm_read_uses_exact_event_cursor_without_reloading(self) -> None:
        client = mock.Mock()
        stream = mock.MagicMock()
        stream.__iter__.return_value = iter(())
        client.events.return_value = stream
        loader = mock.Mock(return_value=CURRENT)
        values = iter((1_000_000_001, 1_000_000_999))
        cache = inventory.LocalSnapshotInventory(
            client,
            "linux/amd64",
            loader=loader,
            clock_ns=lambda: next(values),
            monotonic=mock.Mock(side_effect=(0.0, 1.0)),
        )

        self.assertEqual(cache.candidates(), CURRENT)
        self.assertEqual(cache.candidates(), CURRENT)

        loader.assert_called_once_with(client, "linux/amd64")
        client.events.assert_called_once_with(
            since="1.000000001",
            until="1.000000999",
            filters={"type": "image"},
            decode=True,
        )
        stream.close.assert_called_once_with()

    def test_image_event_refreshes_before_returning(self) -> None:
        client = mock.Mock()
        client.events.return_value = ({"Type": "image", "Action": "create"},)
        loader = mock.Mock(side_effect=(EMPTY, CURRENT))
        values = iter((100, 200, 300))
        cache = inventory.LocalSnapshotInventory(
            client,
            "linux/amd64",
            loader=loader,
            clock_ns=lambda: next(values),
            monotonic=mock.Mock(return_value=0.0),
        )

        self.assertEqual(cache.candidates(), EMPTY)
        self.assertEqual(cache.candidates(), CURRENT)
        self.assertEqual(loader.call_count, 2)

    def test_validator_failure_degrades_to_direct_refresh(self) -> None:
        client = mock.Mock()
        client.events.side_effect = DockerException("events unavailable")
        loader = mock.Mock(side_effect=(EMPTY, CURRENT))
        values = iter((100, 200, 300))
        cache = inventory.LocalSnapshotInventory(
            client,
            "linux/amd64",
            loader=loader,
            clock_ns=lambda: next(values),
            monotonic=mock.Mock(return_value=0.0),
        )

        self.assertEqual(cache.candidates(), EMPTY)
        with self.assertLogs("shimpz-team-local-snapshot-inventory", level="WARNING"):
            self.assertEqual(cache.candidates(), CURRENT)

    def test_partial_event_stream_failure_degrades_to_direct_refresh(self) -> None:
        class BrokenEvents:
            def __init__(self) -> None:
                self.closed = False

            def __iter__(self):
                yield from ()
                raise OSError("truncated")

            def close(self) -> None:
                self.closed = True

        client = mock.Mock()
        events = BrokenEvents()
        client.events.return_value = events
        loader = mock.Mock(side_effect=(EMPTY, CURRENT))
        values = iter((100, 200, 300))
        cache = inventory.LocalSnapshotInventory(
            client,
            "linux/amd64",
            loader=loader,
            clock_ns=lambda: next(values),
            monotonic=mock.Mock(return_value=0.0),
        )

        self.assertEqual(cache.candidates(), EMPTY)
        with self.assertLogs("shimpz-team-local-snapshot-inventory", level="WARNING"):
            self.assertEqual(cache.candidates(), CURRENT)
        self.assertTrue(events.closed)

    def test_expired_cache_refreshes_before_returning_when_old_events_aged_out(self) -> None:
        client = mock.Mock()
        client.events.return_value = ()
        loader = mock.Mock(side_effect=(EMPTY, CURRENT))
        values = iter((100, 200))
        cache = inventory.LocalSnapshotInventory(
            client,
            "linux/amd64",
            loader=loader,
            clock_ns=lambda: next(values),
            monotonic=mock.Mock(side_effect=(0.0, 31.0, 32.0)),
        )

        self.assertEqual(cache.candidates(), EMPTY)
        self.assertEqual(cache.candidates(), CURRENT)

        self.assertEqual(loader.call_count, 2)
        client.events.assert_not_called()

    def test_expired_cache_failure_never_serves_stale_candidates(self) -> None:
        client = mock.Mock()
        loader = mock.Mock(side_effect=(EMPTY, snapshots.LocalSnapshotUnavailableError("offline")))
        cache = inventory.LocalSnapshotInventory(
            client,
            "linux/amd64",
            loader=loader,
            clock_ns=mock.Mock(side_effect=(100, 200, 300)),
            monotonic=mock.Mock(side_effect=(0.0, 31.0)),
        )
        self.assertEqual(cache.candidates(), EMPTY)

        with self.assertRaisesRegex(snapshots.LocalSnapshotUnavailableError, "offline"):
            cache.candidates()

        with cache._condition:
            self.assertFalse(cache._refreshing)
            self.assertEqual(cache._candidates, EMPTY)
        cache._loader = mock.Mock(return_value=CURRENT)
        cache._monotonic = mock.Mock(side_effect=(31.0, 32.0))
        self.assertEqual(cache.candidates(), CURRENT)
        client.events.assert_not_called()

    def test_concurrent_expired_readers_single_flight_without_serving_stale_candidates(self) -> None:
        client = mock.Mock()
        client.events.return_value = ()
        refresh_started = threading.Event()
        release_refresh = threading.Event()
        calls = 0

        def load(_client, _platform):
            nonlocal calls
            calls += 1
            if calls == 1:
                return EMPTY
            refresh_started.set()
            self.assertTrue(release_refresh.wait(timeout=1))
            return CURRENT

        cache = inventory.LocalSnapshotInventory(
            client,
            "linux/amd64",
            loader=load,
            clock_ns=mock.Mock(side_effect=range(100, 110)),
            monotonic=mock.Mock(return_value=0.0),
        )
        self.assertEqual(cache.candidates(), EMPTY)
        cache._monotonic = mock.Mock(return_value=31.0)

        with ThreadPoolExecutor(max_workers=4) as executor:
            futures = [executor.submit(cache.candidates) for _ in range(4)]
            self.assertTrue(refresh_started.wait(timeout=1))
            release_refresh.set()
            self.assertEqual([future.result(timeout=1) for future in futures], [CURRENT] * 4)

        self.assertEqual(calls, 2)

    def test_warmup_single_flies_concurrent_cold_readers(self) -> None:
        client = mock.Mock()
        client.events.return_value = ()
        started = threading.Event()
        release = threading.Event()
        calls = 0

        def load(_client, _platform):
            nonlocal calls
            calls += 1
            started.set()
            self.assertTrue(release.wait(timeout=1))
            return CURRENT

        cache = inventory.LocalSnapshotInventory(client, "linux/amd64", loader=load)
        cache.warm()
        self.assertTrue(started.wait(timeout=1))
        with ThreadPoolExecutor(max_workers=4) as executor:
            futures = [executor.submit(cache.candidates) for _ in range(4)]
            release.set()
            self.assertEqual([future.result(timeout=1) for future in futures], [CURRENT] * 4)
        self.assertEqual(calls, 1)

    def test_failed_warmup_does_not_poison_the_next_read(self) -> None:
        client = mock.Mock()
        failed = threading.Event()

        def unavailable(_client, _platform):
            failed.set()
            raise snapshots.LocalSnapshotUnavailableError("offline")

        cache = inventory.LocalSnapshotInventory(client, "linux/amd64", loader=unavailable)
        cache.warm()
        self.assertTrue(failed.wait(timeout=1))
        with cache._condition:
            self.assertTrue(cache._condition.wait_for(lambda: not cache._refreshing, timeout=1))
        cache._loader = mock.Mock(return_value=CURRENT)

        self.assertEqual(cache.candidates(), CURRENT)

    def test_clock_rollback_forces_refresh(self) -> None:
        client = mock.Mock()
        loader = mock.Mock(side_effect=(EMPTY, CURRENT))
        values = iter((200, 100, 300))
        cache = inventory.LocalSnapshotInventory(
            client,
            "linux/amd64",
            loader=loader,
            clock_ns=lambda: next(values),
            monotonic=mock.Mock(return_value=0.0),
        )

        self.assertEqual(cache.candidates(), EMPTY)
        self.assertEqual(cache.candidates(), CURRENT)
        client.events.assert_not_called()

    def test_retry_observes_a_refresh_completed_by_another_reader(self) -> None:
        cache = inventory.LocalSnapshotInventory(
            mock.Mock(),
            "linux/amd64",
            loader=mock.Mock(return_value=CURRENT),
            clock_ns=mock.Mock(return_value=100),
            monotonic=mock.Mock(return_value=0.0),
        )
        self.assertEqual(cache.candidates(), CURRENT)
        with (
            mock.patch.object(cache, "_validate", side_effect=((True, 200), (False, 300))),
            mock.patch.object(cache, "_claim_refresh", return_value=False),
        ):
            self.assertEqual(cache.candidates(), CURRENT)

    def test_retry_discards_a_snapshot_replaced_before_freshness_check(self) -> None:
        cache = inventory.LocalSnapshotInventory(
            mock.Mock(),
            "linux/amd64",
            loader=mock.Mock(return_value=CURRENT),
            clock_ns=mock.Mock(return_value=100),
            monotonic=mock.Mock(return_value=0.0),
        )
        self.assertEqual(cache.candidates(), CURRENT)
        with (
            mock.patch.object(
                cache,
                "_snapshot_or_claim_cold_refresh",
                side_effect=((CURRENT, 99), (CURRENT, 100)),
            ),
            mock.patch.object(cache, "_validate", return_value=(False, 101)) as validate,
        ):
            self.assertEqual(cache.candidates(), CURRENT)
        validate.assert_called_once_with(100)

    def test_retry_discards_a_validator_result_for_an_older_cursor(self) -> None:
        cache = inventory.LocalSnapshotInventory(
            mock.Mock(),
            "linux/amd64",
            loader=mock.Mock(return_value=CURRENT),
            clock_ns=mock.Mock(return_value=100),
            monotonic=mock.Mock(return_value=0.0),
        )
        self.assertEqual(cache.candidates(), CURRENT)
        validations = 0

        def validate(cursor_ns: int) -> tuple[bool, int]:
            nonlocal validations
            validations += 1
            if validations == 1:
                cache._cursor_ns = cursor_ns + 1
            return False, cursor_ns + 2

        with mock.patch.object(cache, "_validate", side_effect=validate):
            self.assertEqual(cache.candidates(), CURRENT)
        self.assertEqual(validations, 2)

    def test_refresh_waits_are_bounded_and_cursor_fenced(self) -> None:
        cache = inventory.LocalSnapshotInventory(mock.Mock(), "linux/amd64", loader=mock.Mock(return_value=CURRENT))
        cache._refreshing = True
        with (
            mock.patch.object(cache._condition, "wait", return_value=False),
            self.assertRaisesRegex(snapshots.LocalSnapshotUnavailableError, "timed out"),
        ):
            cache._snapshot_or_claim_cold_refresh()

        cache._candidates = CURRENT
        cache._cursor_ns = 100
        self.assertFalse(cache._claim_refresh(99))
        with mock.patch.object(cache._condition, "wait", return_value=True):
            self.assertFalse(cache._claim_refresh(100))
        with (
            mock.patch.object(cache._condition, "wait", return_value=False),
            self.assertRaisesRegex(snapshots.LocalSnapshotUnavailableError, "timed out"),
        ):
            cache._claim_refresh(100)

    def test_background_start_is_idempotent_and_recovers_from_thread_failure(self) -> None:
        cache = inventory.LocalSnapshotInventory(mock.Mock(), "linux/amd64", loader=mock.Mock(return_value=CURRENT))
        cache._refreshing = True
        with mock.patch.object(inventory.threading, "Thread") as thread:
            cache._start_background_locked()
        thread.assert_not_called()

        cache._refreshing = False
        with mock.patch.object(inventory.threading, "Thread") as thread:
            thread.return_value.start.side_effect = RuntimeError("unavailable")
            cache.warm()
        self.assertFalse(cache._refreshing)

    def test_warm_is_a_noop_after_inventory_is_available(self) -> None:
        cache = inventory.LocalSnapshotInventory(mock.Mock(), "linux/amd64", loader=mock.Mock(return_value=CURRENT))
        self.assertEqual(cache.candidates(), CURRENT)
        with mock.patch.object(cache, "_start_background_locked") as start:
            cache.warm()
        start.assert_not_called()


if __name__ == "__main__":
    unittest.main()
