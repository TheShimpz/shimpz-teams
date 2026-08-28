"""Durable current-contract transactions for Assistant replacement."""

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

_UPDATE_FORMAT_VERSION = 2
_RESIDUE_FORMAT_VERSION = 1
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


@dataclass(frozen=True, slots=True)
class AssistantResidue:
    image_id: str


class AssistantUpdateStore:
    """One atomic transaction file per Team-owned Assistant."""

    def __init__(
        self,
        root: Path,
        *,
        local_record_validator: bindings.LocalRecordValidator | None = None,
    ) -> None:
        self._root = root
        self._local_record_validator = local_record_validator

    def begin(
        self,
        previous: bindings.DynamicAssistantBinding,
        successor_document: dict[str, Any],
        previous_image_id: str,
    ) -> AssistantUpdate:
        successor = _successor_binding(previous, successor_document, self._local_record_validator)
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
                if self._path(update.team_id, update.assistant_id) != path:
                    raise bindings.DynamicAssistantError("Assistant update transaction filename is invalid")
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

    def _read(self, path: Path) -> AssistantUpdate | None:
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
        return _decode(value, self._local_record_validator)


class AssistantResidueStore:
    """A retryable exact-image cleanup queue independent from update transactions."""

    def __init__(self, root: Path) -> None:
        self._root = root
        self._lock_path = root / ".lock"

    def add(self, image_id: str) -> AssistantResidue:
        residue = AssistantResidue(_image_id(image_id))
        path = self._path(residue)
        with _FileLock(self._lock_path, fcntl.LOCK_EX):
            current = self._read(path)
            if current is not None:
                if current == residue:
                    return current
                raise bindings.DynamicAssistantError("Assistant residue changed unexpectedly")
            _write(path, {"version": _RESIDUE_FORMAT_VERSION, "image_id": residue.image_id})
        return residue

    def list(self) -> tuple[AssistantResidue, ...]:
        with _FileLock(self._lock_path, fcntl.LOCK_SH):
            try:
                paths = tuple(sorted(self._root.glob("*.json")))
            except OSError as exc:
                raise bindings.DynamicAssistantError("Assistant residues cannot be listed") from exc
            residues: list[AssistantResidue] = []
            for path in paths:
                residue = self._read(path)
                if residue is not None:
                    if self._path(residue) != path:
                        raise bindings.DynamicAssistantError("Assistant residue filename is invalid")
                    residues.append(residue)
        return tuple(sorted(residues, key=lambda item: item.image_id))

    def clear(self, residue: AssistantResidue) -> None:
        path = self._path(residue)
        with _FileLock(self._lock_path, fcntl.LOCK_EX):
            current = self._read(path)
            if current is None:
                return
            if current != residue:
                raise bindings.DynamicAssistantError("Assistant residue changed unexpectedly")
            try:
                path.unlink()
                _sync_directory(path.parent)
            except OSError as exc:
                raise bindings.DynamicAssistantError("Assistant residue cannot be cleared") from exc

    def _path(self, residue: AssistantResidue) -> Path:
        return self._root / f"{residue.image_id.removeprefix('sha256:')}.json"

    @staticmethod
    def _read(path: Path) -> AssistantResidue | None:
        try:
            raw = path.read_bytes()
        except FileNotFoundError:
            return None
        except OSError as exc:
            raise bindings.DynamicAssistantError("Assistant residue cannot be read") from exc
        if not raw or len(raw) > 1024:
            raise bindings.DynamicAssistantError("Assistant residue is malformed")
        try:
            value = json.loads(raw)
        except (UnicodeError, json.JSONDecodeError) as exc:
            raise bindings.DynamicAssistantError("Assistant residue is malformed") from exc
        if (
            not isinstance(value, dict)
            or set(value) != {"version", "image_id"}
            or value["version"] != _RESIDUE_FORMAT_VERSION
        ):
            raise bindings.DynamicAssistantError("Assistant residue is malformed")
        return AssistantResidue(_image_id(value["image_id"]))


class _FileLock:
    def __init__(self, path: Path, operation: int) -> None:
        self._path = path
        self._operation = operation
        self._stream = None

    def __enter__(self) -> None:
        descriptor = -1
        try:
            self._path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            descriptor = os.open(self._path, os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0), 0o600)
            self._stream = os.fdopen(descriptor, "rb+")
            descriptor = -1
            fcntl.flock(self._stream, self._operation)
        except OSError as exc:
            if descriptor >= 0:
                os.close(descriptor)
            if self._stream is not None:
                self._stream.close()
                self._stream = None
            raise bindings.DynamicAssistantError("Assistant update state lock is unavailable") from exc
        except Exception:
            if descriptor >= 0:
                os.close(descriptor)
            if self._stream is not None:
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
        "version": _UPDATE_FORMAT_VERSION,
        "team_id": update.team_id,
        "assistant_id": update.assistant_id,
        "previous": bindings._encode_binding(update.previous),
        "successor": bindings._encode_binding(update.successor),
        "previous_image_id": update.previous_image_id,
    }


def _decode(
    value: object,
    local_record_validator: bindings.LocalRecordValidator | None = None,
) -> AssistantUpdate:
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
        value["version"] != _UPDATE_FORMAT_VERSION
        or not isinstance(value["previous_image_id"], str)
        or _DIGEST_RE.fullmatch(value["previous_image_id"]) is None
    ):
        raise bindings.DynamicAssistantError("Assistant update transaction is malformed")
    previous = bindings._decode_binding(value["previous"], local_record_validator)
    successor = bindings._decode_binding(value["successor"], local_record_validator)
    if previous.assistant_id != value["assistant_id"] or successor.assistant_id != value["assistant_id"]:
        raise bindings.DynamicAssistantError("Assistant update transaction is malformed")
    if previous.team_id != value["team_id"] or successor.team_id != value["team_id"]:
        raise bindings.DynamicAssistantError("Assistant update transaction is malformed")
    if previous.provenance != successor.provenance:
        raise bindings.DynamicAssistantError("Assistant update transaction is malformed")
    return AssistantUpdate(
        previous.team_id,
        previous.assistant_id,
        previous,
        successor,
        value["previous_image_id"],
    )


def _successor_binding(
    previous: bindings.DynamicAssistantBinding,
    document: dict[str, Any],
    local_record_validator: bindings.LocalRecordValidator | None,
) -> bindings.DynamicAssistantBinding:
    if previous.provenance == "published":
        return bindings.binding_from_resolution(previous.team_id, document)
    if previous.provenance == "local":
        return bindings.binding_from_local_record(previous.team_id, document, local_record_validator)
    raise bindings.DynamicAssistantConflictError("the Assistant update provenance is invalid")


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


def _image_id(value: object) -> str:
    if not isinstance(value, str) or _DIGEST_RE.fullmatch(value) is None:
        raise bindings.DynamicAssistantError("Assistant residue image id is invalid")
    return value
