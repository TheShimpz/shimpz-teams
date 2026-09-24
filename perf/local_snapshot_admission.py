"""Attribute Local snapshot admission using one already-staged exact image.

Run from the Teams checkout with ``python -m perf.local_snapshot_admission``.
The production admission creates and removes a never-started offline container.
This benchmark never builds, tags, installs, starts, or deletes an image.
"""

from __future__ import annotations

import json
import math
import time
from collections import defaultdict

import docker
from docker.errors import DockerException, NotFound

from assistant import manifest as assistant_manifest
from local.install import snapshots
from perf.local_snapshot_inventory import _event_count, _percentiles

SAMPLES = 12
SIZE_BUCKET_BYTES = 64 * 1024
ARCHIVE_NAMES = {
    snapshots.SOURCE_PATH: "source_package",
    assistant_manifest.MANIFEST_PATH: "manifest",
    assistant_manifest.CONTRACT_PATH: "contract",
    snapshots.ICON_PATH: "icon",
}


class ConfoundedMeasurementError(RuntimeError):
    """Concurrent image activity or an unexpected admission call invalidated the run."""


class TemporaryContainerResidueError(RuntimeError):
    """Admission left its own temporary container present."""

    def __init__(self, container_id: str) -> None:
        super().__init__("Local snapshot admission left a temporary container")
        self.container_id = container_id


class TemporaryContainerVerificationError(RuntimeError):
    """Docker could not prove that an admission container was removed."""

    def __init__(self, container_id: str) -> None:
        super().__init__("Local snapshot admission container absence could not be verified")
        self.container_id = container_id


class _Recorder:
    def __init__(self) -> None:
        self.spans: dict[str, float] = defaultdict(float)
        self.created_ids: list[str] = []
        self.size_buckets: dict[str, int] = {}

    def timed(self, name, operation, *args, **kwargs):
        started = time.perf_counter_ns()
        try:
            return operation(*args, **kwargs)
        finally:
            self.spans[name] += (time.perf_counter_ns() - started) / 1_000_000


class _ArchiveStream:
    def __init__(self, stream, recorder: _Recorder, name: str) -> None:
        self._stream = iter(stream)
        self._original = stream
        self._recorder = recorder
        self._name = name

    def __iter__(self):
        return self

    def __next__(self):
        return self._recorder.timed(self._name, next, self._stream)

    def close(self) -> None:
        close = getattr(self._original, "close", None)
        if callable(close):
            self._recorder.timed(self._name, close)


class _Container:
    def __init__(self, container, recorder: _Recorder) -> None:
        self._container = container
        self._recorder = recorder

    def get_archive(self, path: str):
        name = ARCHIVE_NAMES.get(path)
        if name is None:
            raise ConfoundedMeasurementError("admission requested an unexpected archive")
        stream, metadata = self._recorder.timed(f"{name}_request", self._container.get_archive, path)
        size = metadata.get("size") if isinstance(metadata, dict) else None
        if isinstance(size, int) and not isinstance(size, bool) and size >= 0:
            self._recorder.size_buckets[name] = math.ceil(size / SIZE_BUCKET_BYTES) * SIZE_BUCKET_BYTES
        return _ArchiveStream(stream, self._recorder, f"{name}_stream"), metadata

    def remove(self, *args, **kwargs):
        return self._recorder.timed("container_remove", self._container.remove, *args, **kwargs)

    def __getattr__(self, name):
        raise ConfoundedMeasurementError(f"admission accessed an unexpected container operation: {name}")


class _Containers:
    def __init__(self, containers, recorder: _Recorder) -> None:
        self._containers = containers
        self._recorder = recorder

    def create(self, *args, **kwargs):
        container = self._recorder.timed("container_create", self._containers.create, *args, **kwargs)
        self._recorder.created_ids.append(container.id)
        return _Container(container, self._recorder)

    def __getattr__(self, name):
        raise ConfoundedMeasurementError(f"admission accessed an unexpected container collection operation: {name}")


class _Images:
    def __init__(self, images, recorder: _Recorder) -> None:
        self._images = images
        self._recorder = recorder

    def get(self, image_id: str):
        return self._recorder.timed("image_get", self._images.get, image_id)

    def __getattr__(self, name):
        raise ConfoundedMeasurementError(f"admission accessed an unexpected image collection operation: {name}")


class _Client:
    def __init__(self, client: docker.DockerClient, recorder: _Recorder) -> None:
        self._client = client
        self._recorder = recorder
        self.images = _Images(client.images, recorder)
        self.containers = _Containers(client.containers, recorder)

    def info(self):
        return self._recorder.timed("daemon_info", self._client.info)

    def __getattr__(self, name):
        raise ConfoundedMeasurementError(f"admission accessed an unexpected Docker client operation: {name}")


def _assert_removed(client: docker.DockerClient, created_ids: list[str]) -> None:
    for container_id in created_ids:
        try:
            client.containers.get(container_id)
        except NotFound:
            continue
        except (DockerException, OSError) as exc:
            raise TemporaryContainerVerificationError(container_id) from exc
        raise TemporaryContainerResidueError(container_id)


def _sample(client: docker.DockerClient, image_id: str):
    recorder = _Recorder()
    started = time.perf_counter_ns()
    try:
        admitted = snapshots.admit(_Client(client, recorder), image_id)
        total_ms = (time.perf_counter_ns() - started) / 1_000_000
    finally:
        _assert_removed(client, recorder.created_ids)
    if len(recorder.created_ids) != 1 or len(recorder.size_buckets) != len(ARCHIVE_NAMES):
        raise ConfoundedMeasurementError("admission did not use its expected Docker calls")
    other_ms = total_ms - sum(recorder.spans.values())
    return admitted, total_ms, other_ms, recorder


def _measure(client: docker.DockerClient):
    candidates = snapshots.list_candidates(client)
    if len(candidates) != 1:
        raise ConfoundedMeasurementError("benchmark requires exactly one current staged image")
    image_id = candidates[0].image_id
    image_count_before = len(client.api.images(all=True))
    started_ns = time.time_ns()
    first, cold_ms, _, cold_recorder = _sample(client, image_id)
    timings: dict[str, list[float]] = defaultdict(list)
    buckets = cold_recorder.size_buckets
    for _ in range(SAMPLES):
        admitted, total_ms, other_ms, recorder = _sample(client, image_id)
        if admitted != first or recorder.size_buckets != buckets:
            raise ConfoundedMeasurementError("the exact image changed during admission")
        timings["total"].append(total_ms)
        timings["other_wall"].append(other_ms)
        for name, elapsed in recorder.spans.items():
            timings[name].append(elapsed)
    image_count_after = len(client.api.images(all=True))
    events = _event_count(client, started_ns, time.time_ns())
    if image_count_before != image_count_after or events != 0:
        raise ConfoundedMeasurementError("Docker image activity changed during admission")
    if any(len(values) != SAMPLES for values in timings.values()):
        raise ConfoundedMeasurementError("admission call counts changed between samples")
    return {
        "status": "valid",
        "candidate_count": len(candidates),
        "image_events": events,
        "host_images_before": image_count_before,
        "host_images_after": image_count_after,
        "cold_total_ms": round(cold_ms, 2),
        "failure_count": 0,
        "size_bucket_width_kib": SIZE_BUCKET_BYTES // 1024,
        "size_bucket_kib": {name: size // 1024 for name, size in sorted(buckets.items())},
        "spans": {name: _percentiles(values) for name, values in sorted(timings.items())},
        "scope": "one host Docker staged image; excludes HTTP, Supervisor auth, and Assistant start",
    }


def main() -> int:
    client = docker.from_env()
    try:
        result = _measure(client)
    except TemporaryContainerResidueError as exc:
        print(json.dumps({"status": "residue", "container_id": exc.container_id}))
        return 1
    except TemporaryContainerVerificationError as exc:
        print(json.dumps({"status": "residue_unknown", "container_id": exc.container_id}))
        return 1
    except ConfoundedMeasurementError:
        print(json.dumps({"status": "confounded"}))
        return 2
    except (DockerException, snapshots.LocalSnapshotError, OSError, RuntimeError, TypeError, ValueError) as exc:
        print(json.dumps({"status": "error", "error_type": type(exc).__name__}))
        return 1
    finally:
        client.close()
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
