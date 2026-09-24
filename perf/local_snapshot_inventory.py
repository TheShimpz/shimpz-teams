"""Measure Local snapshot discovery against a real, read-only Docker daemon.

Run from the Teams checkout with ``python -m perf.local_snapshot_inventory``.
Add ``--filter-attribution`` to compare the exact stage-label Docker filter
against nonmatching labels and an unfiltered listing on one stable image set.
Only timing and counts are printed; no image or Assistant metadata is emitted.
"""

from __future__ import annotations

import argparse
import json
import math
import secrets
import time

import docker
from docker.errors import DockerException

from local.install import inventory, snapshots

SAMPLES = 12
WARM_GAP_SECONDS = 2
REAL_EXPIRY_SAMPLES = 3
REAL_EXPIRY_WAIT_SECONDS = inventory.MAX_CACHE_AGE_SECONDS + 1


class ConfoundedMeasurementError(RuntimeError):
    """Image activity or candidate drift invalidated the measurement."""


def _percentiles(values: list[float]) -> dict[str, float | int]:
    ordered = sorted(values)
    return {
        "n": len(ordered),
        "p50_ms": round(ordered[math.ceil(len(ordered) * 0.50) - 1], 2),
        "p95_ms": round(ordered[math.ceil(len(ordered) * 0.95) - 1], 2),
    }


def _timed(cache: inventory.LocalSnapshotInventory) -> tuple[float, int]:
    started = time.perf_counter_ns()
    candidates = cache.candidates()
    return (time.perf_counter_ns() - started) / 1_000_000, len(candidates)


def _event_count(client: docker.DockerClient, start_ns: int, end_ns: int) -> int:
    stream = client.events(
        since=inventory._docker_timestamp(start_ns),
        until=inventory._docker_timestamp(end_ns),
        filters={"type": "image"},
        decode=True,
    )
    try:
        return sum(1 for _ in stream)
    finally:
        close = getattr(stream, "close", None)
        if callable(close):
            close()


def _samples(client: docker.DockerClient, platform: str) -> dict[str, object]:
    load_count = 0

    def load(source: object, target_platform: str) -> tuple[snapshots.LocalSnapshotCandidate, ...]:
        nonlocal load_count
        load_count += 1
        return snapshots.list_candidates(source, platform=target_platform)

    fake_age = [0.0]
    cache = inventory.LocalSnapshotInventory(client, platform, loader=load, monotonic=lambda: fake_age[0])
    expired_ms: list[float] = []
    warm_ms: list[float] = []
    cold_start_ms, count = _timed(cache)
    candidate_counts = {count}
    for _ in range(SAMPLES):
        fake_age[0] += REAL_EXPIRY_WAIT_SECONDS
        before = load_count
        elapsed, count = _timed(cache)
        if load_count != before + 1:
            raise RuntimeError("expired inventory did not refresh exactly once")
        expired_ms.append(elapsed)
        candidate_counts.add(count)

        time.sleep(WARM_GAP_SECONDS)
        before = load_count
        elapsed, count = _timed(cache)
        if load_count != before:
            raise ConfoundedMeasurementError("image activity refreshed a warm inventory sample")
        warm_ms.append(elapsed)
        candidate_counts.add(count)

    real_cache = inventory.LocalSnapshotInventory(client, platform, loader=load)
    _, count = _timed(real_cache)
    candidate_counts.add(count)
    real_expiry_ms: list[float] = []
    for _ in range(REAL_EXPIRY_SAMPLES):
        time.sleep(REAL_EXPIRY_WAIT_SECONDS)
        before = load_count
        elapsed, count = _timed(real_cache)
        if load_count != before + 1:
            raise RuntimeError("real-age inventory did not refresh exactly once")
        real_expiry_ms.append(elapsed)
        candidate_counts.add(count)

    if len(candidate_counts) != 1:
        raise ConfoundedMeasurementError("candidate count changed during measurement")
    return {
        "candidate_count": candidate_counts.pop(),
        "cold_start_ms": round(cold_start_ms, 2),
        "expired_synthetic": _percentiles(expired_ms),
        "warm_after_2s": _percentiles(warm_ms),
        "expired_real_31s": _percentiles(real_expiry_ms),
    }


def _filter_samples(client: docker.DockerClient) -> dict[str, object]:
    nonce = secrets.token_hex(16)
    filters = {
        "stage_label": {"label": [f"{snapshots.LOCAL_STAGE_LABEL}={snapshots.LOCAL_STAGE_VALUE}"]},
        "absent_key": {"label": [f"org.shimpz.perf.never.{nonce}=1"]},
        "absent_value": {"label": [f"{snapshots.LOCAL_STAGE_LABEL}=absent-{nonce}"]},
        "unfiltered": None,
    }
    elapsed: dict[str, list[float]] = {name: [] for name in filters}
    counts: dict[str, set[int]] = {name: set() for name in filters}
    names = tuple(filters)

    def list_images(name: str):
        selected = filters[name]
        return client.api.images(all=True, filters=selected) if selected is not None else client.api.images(all=True)

    for name in names:
        if not isinstance(list_images(name), list):
            raise ConfoundedMeasurementError("Docker returned an invalid image inventory")
    for cycle in range(SAMPLES):
        for name in (*names[cycle % len(names) :], *names[: cycle % len(names)]):
            started = time.perf_counter_ns()
            images = list_images(name)
            elapsed[name].append((time.perf_counter_ns() - started) / 1_000_000)
            if not isinstance(images, list):
                raise ConfoundedMeasurementError("Docker returned an invalid image inventory")
            counts[name].add(len(images))
    if any(len(values) != 1 for values in counts.values()):
        raise ConfoundedMeasurementError("image count changed during the filter comparison")
    if counts["absent_key"] != {0} or counts["absent_value"] != {0}:
        raise ConfoundedMeasurementError("a nonmatching filter returned an image")
    return {
        "filter_arms": {name: {"result_count": counts[name].pop(), **_percentiles(elapsed[name])} for name in names},
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--filter-attribution", action="store_true")
    args = parser.parse_args()
    client = docker.from_env()
    try:
        platform = snapshots.platform_from_info(client.info())
        started_ns = time.time_ns()
        image_count_before = len(client.api.images(all=True))
        samples = _filter_samples(client) if args.filter_attribution else _samples(client, platform)
        image_count_after = len(client.api.images(all=True))
        events = _event_count(client, started_ns, time.time_ns())
    except ConfoundedMeasurementError:
        print(json.dumps({"status": "confounded"}))
        return 2
    # A malformed staged image error may contain its immutable ID; never print its message.
    except (DockerException, snapshots.LocalSnapshotError, OSError, RuntimeError, TypeError, ValueError) as exc:
        print(json.dumps({"status": "error", "error_type": type(exc).__name__}))
        return 1
    finally:
        client.close()

    valid = events == 0 and image_count_before == image_count_after
    print(
        json.dumps(
            {
                "status": "valid" if valid else "confounded",
                "mode": "filter-attribution" if args.filter_attribution else "cache-refresh",
                "scope": "host Docker inventory; excludes HTTP, Supervisor auth, provider, and container CPU limits",
                "platform": platform,
                "host_images_before": image_count_before,
                "host_images_after": image_count_after,
                "image_events": events,
                **samples,
            },
            sort_keys=True,
        )
    )
    return 0 if valid else 2


if __name__ == "__main__":
    raise SystemExit(main())
