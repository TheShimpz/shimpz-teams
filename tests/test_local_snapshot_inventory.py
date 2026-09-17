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

    def test_age_ceiling_returns_cache_and_refreshes_in_background(self) -> None:
        client = mock.Mock()
        client.events.return_value = ()
        refresh_started = threading.Event()
        release_refresh = threading.Event()
        loaded_at_updated = threading.Event()
        calls = 0

        def load(_client, _platform):
            nonlocal calls
            calls += 1
            if calls == 1:
                return EMPTY
            refresh_started.set()
            self.assertTrue(release_refresh.wait(timeout=1))
            return CURRENT

        monotonic_values = iter((0.0, 31.0, 32.0))

        def monotonic() -> float:
            value = next(monotonic_values)
            if value == 32.0:
                loaded_at_updated.set()
            return value

        values = iter((100, 200, 300))
        cache = inventory.LocalSnapshotInventory(
            client,
            "linux/amd64",
            loader=load,
            clock_ns=lambda: next(values),
            monotonic=monotonic,
        )

        self.assertEqual(cache.candidates(), EMPTY)
        self.assertEqual(cache.candidates(), EMPTY)
        self.assertTrue(refresh_started.wait(timeout=1))
        release_refresh.set()
        self.assertTrue(loaded_at_updated.wait(timeout=1))
        with cache._condition:
            self.assertEqual(cache._candidates, CURRENT)
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


if __name__ == "__main__":
    unittest.main()
