"""Durable current-contract transactions for Assistant publication replacement."""

from __future__ import annotations

import fcntl
import json
import os
import re
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from install import bindings

_FORMAT_VERSION = 1
_MAX_BYTES = 2 * 1024 * 1024
_TEAM_ID_RE = re.compile(r"^[a-z0-9_]{1,40}$")
_ASSISTANT_ID_RE = re.compile(r"^[a-z][a-z0-9]*(?:-[a-z0-9]+)*$")
_DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")


@dataclass(frozen=True, slots=True)
class AssistantUpdate:
    team_id: str
    assistant_id: str
    previous: bindings.DynamicAssistantBinding
    successor: bindings.DynamicAssistantBinding
    previous_image_id: str


class AssistantUpdateStore:
    """One atomic transaction file per Team-owned Assistant."""

    def __init__(self, root: Path) -> None:
        self._root = root

    def begin(
        self,
        previous: bindings.DynamicAssistantBinding,
        successor_resolution: dict[str, Any],
        previous_image_id: str,
    ) -> AssistantUpdate:
        successor = bindings.binding_from_resolution(previous.team_id, successor_resolution)
        if successor.assistant_id != previous.assistant_id or successor == previous:
            raise bindings.DynamicAssistantConflictError("the Assistant update transaction is invalid")
        if _DIGEST_RE.fullmatch(previous_image_id) is None:
            raise bindings.DynamicAssistantConflictError("the previous Assistant image id is invalid")
        update = AssistantUpdate(previous.team_id, previous.assistant_id, previous, successor, previous_image_id)
        path = self._path(update.team_id, update.assistant_id)
        with _FileLock(path.with_suffix(".lock"), fcntl.LOCK_EX):
            current = self._read(path)
            if current is not None:
                if current == update:
                    return current
                raise bindings.DynamicAssistantConflictError("another Assistant update is already active")
            _write(path, _encode(update))
        return update

    def get(self, team_id: str, assistant_id: str) -> AssistantUpdate | None:
        path = self._path(team_id, assistant_id)
        with _FileLock(path.with_suffix(".lock"), fcntl.LOCK_SH):
            return self._read(path)

    def list(self) -> tuple[AssistantUpdate, ...]:
        try:
            paths = tuple(sorted(self._root.glob("*.json")))
        except OSError as exc:
            raise bindings.DynamicAssistantError("Assistant update transactions cannot be listed") from exc
        updates: list[AssistantUpdate] = []
        for path in paths:
            with _FileLock(path.with_suffix(".lock"), fcntl.LOCK_SH):
                update = self._read(path)
            if update is not None:
                updates.append(update)
        return tuple(sorted(updates, key=lambda item: (item.team_id, item.assistant_id)))

    def clear(self, update: AssistantUpdate) -> None:
        path = self._path(update.team_id, update.assistant_id)
        with _FileLock(path.with_suffix(".lock"), fcntl.LOCK_EX):
            current = self._read(path)
            if current is None:
                return
            if current != update:
                raise bindings.DynamicAssistantConflictError("the Assistant update transaction changed")
            try:
                path.unlink()
                _sync_directory(path.parent)
            except OSError as exc:
                raise bindings.DynamicAssistantError("Assistant update transaction cannot be cleared") from exc

    def _path(self, team_id: str, assistant_id: str) -> Path:
        if (
            not isinstance(team_id, str)
            or _TEAM_ID_RE.fullmatch(team_id) is None
            or not isinstance(assistant_id, str)
            or len(assistant_id) > 40
            or _ASSISTANT_ID_RE.fullmatch(assistant_id) is None
        ):
            raise bindings.DynamicAssistantError("Assistant update identity is invalid")
        return self._root / f"{team_id}--{assistant_id}.json"

    @staticmethod
    def _read(path: Path) -> AssistantUpdate | None:
        try:
            raw = path.read_bytes()
        except FileNotFoundError:
            return None
        except OSError as exc:
            raise bindings.DynamicAssistantError("Assistant update transaction cannot be read") from exc
        if not raw or len(raw) > _MAX_BYTES:
            raise bindings.DynamicAssistantError("Assistant update transaction is malformed")
        try:
            value = json.loads(raw)
        except (UnicodeError, json.JSONDecodeError) as exc:
            raise bindings.DynamicAssistantError("Assistant update transaction is malformed") from exc
        return _decode(value)


class _FileLock:
    def __init__(self, path: Path, operation: int) -> None:
        self._path = path
        self._operation = operation
        self._stream = None

    def __enter__(self) -> None:
        self._path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        descriptor = os.open(self._path, os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0), 0o600)
        try:
            self._stream = os.fdopen(descriptor, "rb+")
            fcntl.flock(self._stream, self._operation)
        except Exception:
            if self._stream is None:
                os.close(descriptor)
            else:
                self._stream.close()
                self._stream = None
            raise

    def __exit__(self, *_args: object) -> None:
        if self._stream is None:
            raise bindings.DynamicAssistantError("Assistant update transaction lock is unavailable")
        try:
            fcntl.flock(self._stream, fcntl.LOCK_UN)
        finally:
            self._stream.close()


def _encode(update: AssistantUpdate) -> dict[str, object]:
    return {
        "version": _FORMAT_VERSION,
        "team_id": update.team_id,
        "assistant_id": update.assistant_id,
        "previous": update.previous.resolution,
        "successor": update.successor.resolution,
        "previous_image_id": update.previous_image_id,
    }


def _decode(value: object) -> AssistantUpdate:
    if not isinstance(value, dict) or set(value) != {
        "version",
        "team_id",
        "assistant_id",
        "previous",
        "successor",
        "previous_image_id",
    }:
        raise bindings.DynamicAssistantError("Assistant update transaction is malformed")
    if (
        value["version"] != _FORMAT_VERSION
        or not isinstance(value["previous"], dict)
        or not isinstance(value["successor"], dict)
        or not isinstance(value["previous_image_id"], str)
        or _DIGEST_RE.fullmatch(value["previous_image_id"]) is None
    ):
        raise bindings.DynamicAssistantError("Assistant update transaction is malformed")
    previous = bindings.binding_from_resolution(value["team_id"], value["previous"])
    successor = bindings.binding_from_resolution(value["team_id"], value["successor"])
    if previous.assistant_id != value["assistant_id"] or successor.assistant_id != value["assistant_id"]:
        raise bindings.DynamicAssistantError("Assistant update transaction is malformed")
    return AssistantUpdate(
        previous.team_id,
        previous.assistant_id,
        previous,
        successor,
        value["previous_image_id"],
    )


def _write(path: Path, value: dict[str, object]) -> None:
    encoded = json.dumps(value, allow_nan=False, separators=(",", ":"), sort_keys=True).encode()
    temporary: Path | None = None
    try:
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(mode="wb", dir=path.parent, prefix=f".{path.name}.", delete=False) as stream:
            temporary = Path(stream.name)
            temporary.chmod(0o600)
            stream.write(encoded)
            stream.flush()
            os.fsync(stream.fileno())
        temporary.replace(path)
        temporary = None
        _sync_directory(path.parent)
    except OSError as exc:
        raise bindings.DynamicAssistantError("Assistant update transaction cannot be written") from exc
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def _sync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
