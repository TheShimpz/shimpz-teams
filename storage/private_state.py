"""Shared fail-closed plumbing for encrypted Team-owned state."""

from __future__ import annotations

import base64
import json
import os
import secrets
import stat
from collections.abc import Mapping
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

_DIRECTORY_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC


@dataclass(frozen=True, slots=True)
class PrivateFileIdentity:
    device: int
    inode: int
    size: int
    mtime_ns: int
    ctime_ns: int


@dataclass(frozen=True, slots=True)
class PrivateFileRead:
    identity: PrivateFileIdentity | None
    payload: bytes | None
    unchanged: bool


def timestamp() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def empty_state() -> dict[str, object]:
    # last_generation is store-wide and survives every deletion, so a replaced secret never reuses the generation a
    # prepared Action batch bound before its predecessor was removed.
    return {"schema": 1, "teams": {}, "last_generation": 0}


def fsync_directory(path: Path) -> None:
    """Commit the entries of directory ``path``, never through a symbolic link; any failure raises ``OSError``."""
    directory = os.open(path, _DIRECTORY_FLAGS)
    try:
        os.fsync(directory)
    finally:
        os.close(directory)


def replace_durably(path: Path, payload: bytes, *, mode: int = 0o600, group: int | None = None) -> None:
    """Atomically and durably replace ``path`` with ``payload``; any failure raises ``OSError``."""
    directory = os.open(path.parent, _DIRECTORY_FLAGS)
    try:
        replace_in_directory(directory, path.name, payload, mode=mode, group=group)
    finally:
        os.close(directory)


def replace_in_directory(
    directory: int,
    name: str,
    payload: bytes,
    *,
    mode: int = 0o600,
    group: int | None = None,
) -> None:
    """Atomically and durably replace entry ``name`` of the open ``directory``; any failure raises ``OSError``.

    A unique, exclusively created temporary in that same directory receives its final group and mode before any
    byte, is fsynced, replaces ``name``, and the directory is fsynced. A crash therefore leaves the complete prior
    file or the complete new one, never a partial write, and a returned replacement survives power loss.
    """
    temporary = f".{name}.{secrets.token_hex(8)}.tmp"
    descriptor = -1
    try:
        descriptor = os.open(
            temporary,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
            mode,
            dir_fd=directory,
        )
        if group is not None:
            os.fchown(descriptor, -1, group)
        os.fchmod(descriptor, mode)
        view = memoryview(payload)
        while view:
            written = os.write(descriptor, view)
            if written < 1:
                raise OSError("short durable write")
            view = view[written:]
        os.fsync(descriptor)
        os.close(descriptor)
        descriptor = -1
        os.rename(temporary, name, src_dir_fd=directory, dst_dir_fd=directory)
        os.fsync(directory)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        # After a successful replace the temporary is gone; after a failure (for example a read-only volume) its
        # cleanup must not replace the original persistence error.
        with suppress(OSError):
            os.unlink(temporary, dir_fd=directory)


def seal(key: bytes, plaintext: bytes, aad: bytes) -> dict[str, str]:
    """Encrypt ``plaintext`` bound to ``aad`` under a fresh 96-bit nonce into the stored AES-256-GCM envelope."""
    nonce = os.urandom(12)
    return {
        "algorithm": "AES-256-GCM",
        "nonce": base64.b64encode(nonce).decode("ascii"),
        "ciphertext": base64.b64encode(AESGCM(key).encrypt(nonce, plaintext, aad)).decode("ascii"),
    }


@dataclass(frozen=True, slots=True)
class PrivateState:
    error_class: type[RuntimeError]
    malformed_state: str
    malformed_envelope: str
    maximum_encoded_part: int

    def decode_part(
        self,
        value: object,
        *,
        expected: int | None = None,
        minimum: int | None = None,
        maximum: int | None = None,
    ) -> bytes:
        if not isinstance(value, str) or len(value) > self.maximum_encoded_part:
            raise self.error_class(self.malformed_envelope)
        try:
            decoded = base64.b64decode(value, validate=True)
        except (ValueError, TypeError) as exc:
            raise self.error_class(self.malformed_envelope) from exc
        if (
            (expected is not None and len(decoded) != expected)
            or (minimum is not None and len(decoded) < minimum)
            or (maximum is not None and len(decoded) > maximum)
        ):
            raise self.error_class(self.malformed_envelope)
        return decoded

    def read_private_file_if_changed(
        self,
        path: Path,
        maximum: int,
        label: str,
        previous: PrivateFileIdentity | None,
        *,
        cache_initialized: bool,
    ) -> PrivateFileRead:
        try:
            descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
        except FileNotFoundError:
            return PrivateFileRead(None, None, cache_initialized and previous is None)
        except OSError as exc:
            raise self.error_class(f"{label} is unavailable") from exc
        try:
            metadata = os.fstat(descriptor)
            if (
                not stat.S_ISREG(metadata.st_mode)
                or metadata.st_uid != os.geteuid()
                or metadata.st_nlink != 1
                or stat.S_IMODE(metadata.st_mode) != 0o600
                or metadata.st_size > maximum
            ):
                raise self.error_class(f"{label} failed its ownership contract")
            identity = PrivateFileIdentity(
                metadata.st_dev,
                metadata.st_ino,
                metadata.st_size,
                metadata.st_mtime_ns,
                metadata.st_ctime_ns,
            )
            if cache_initialized and identity == previous:
                return PrivateFileRead(identity, None, True)
            payload = bytearray()
            while len(payload) <= maximum:
                chunk = os.read(descriptor, min(64 * 1024, maximum + 1 - len(payload)))
                if not chunk:
                    break
                payload.extend(chunk)
            if len(payload) > maximum:
                raise self.error_class(f"{label} exceeds its fixed byte limit")
            return PrivateFileRead(identity, bytes(payload), False)
        finally:
            os.close(descriptor)

    def read_private_file(self, path: Path, maximum: int, label: str) -> bytes | None:
        return self.read_private_file_if_changed(
            path,
            maximum,
            label,
            None,
            cache_initialized=False,
        ).payload

    def atomic_write(self, path: Path, payload: bytes, label: str) -> None:
        self.require_private_directory(path.parent, label)
        try:
            replace_durably(path, payload)
        except OSError as exc:
            raise self.error_class(f"{label} could not be persisted") from exc

    def write_json(self, path: Path, value: object, maximum: int, label: str) -> None:
        """Persist ``value`` as canonical ASCII JSON of at most ``maximum`` bytes."""
        payload = json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":")).encode("ascii")
        if len(payload) > maximum:
            raise self.error_class(f"{label} exceeds its fixed byte limit")
        self.atomic_write(path, payload, label)

    def separate_paths(self, state_path: Path, key_path: Path, label: str) -> tuple[Path, Path]:
        """Require absolute state and keyring paths whose parents are different directories."""
        state, key = Path(state_path), Path(key_path)
        if not state.is_absolute() or not key.is_absolute():
            raise self.error_class(f"{label} state and key paths must be absolute")
        try:
            shared = state.parent.resolve() == key.parent.resolve()
        except OSError as exc:
            raise self.error_class(f"{label} storage paths are unavailable") from exc
        if shared:
            raise self.error_class(f"{label} keyring must be separate from encrypted state")
        return state, key

    def key(self, path: Path, label: str, *, allow_create: bool = False) -> bytes:
        payload = self.read_private_file(path, 32, label)
        if payload is None:
            if not allow_create:
                raise self.error_class(f"{label} is unavailable")
            payload = AESGCM.generate_key(bit_length=256)
            self.atomic_write(path, payload, label)
        if len(payload) != 32:
            raise self.error_class(f"{label} is invalid")
        return payload

    def check_envelope(self, value: object, maximum_plaintext: int, malformed: str) -> None:
        """Require one AES-256-GCM envelope whose parts fit a plaintext of at most ``maximum_plaintext`` bytes."""
        if (
            not isinstance(value, dict)
            or set(value) != {"algorithm", "nonce", "ciphertext"}
            or value["algorithm"] != "AES-256-GCM"
        ):
            raise self.error_class(malformed)
        self._envelope_parts(value, maximum_plaintext)

    def open_envelope(
        self,
        key: bytes,
        envelope: Mapping[str, object],
        aad: bytes,
        maximum_plaintext: int,
        failure: str,
    ) -> bytes:
        """Authenticate and decrypt an admitted envelope; a failed authentication tag raises ``failure``."""
        nonce, ciphertext = self._envelope_parts(envelope, maximum_plaintext)
        try:
            return AESGCM(key).decrypt(nonce, ciphertext, aad)
        except InvalidTag as exc:
            raise self.error_class(failure) from exc

    def _envelope_parts(self, envelope: Mapping[str, object], maximum_plaintext: int) -> tuple[bytes, bytes]:
        return (
            self.decode_part(envelope.get("nonce"), expected=12),
            self.decode_part(envelope.get("ciphertext"), minimum=17, maximum=maximum_plaintext + 16),
        )

    def records(
        self,
        state: dict[str, object],
        team_id: str,
        assistant_id: str,
        *,
        create: bool,
    ) -> dict[str, object]:
        teams = self._teams(state)
        assistants = teams.get(team_id)
        if assistants is None:
            if not create:
                return {}
            assistants = {}
            teams[team_id] = assistants
        elif not isinstance(assistants, dict):
            raise self.error_class(self.malformed_state)
        records = assistants.get(assistant_id)
        if records is None:
            if not create:
                return {}
            records = {}
            assistants[assistant_id] = records
        elif not isinstance(records, dict):
            raise self.error_class(self.malformed_state)
        return records

    def last_generation(self, state: Mapping[str, object]) -> int:
        last = state.get("last_generation")
        if type(last) is not int or last < 0:
            raise self.error_class(self.malformed_state)
        return last

    def advance_generation(self, state: dict[str, object]) -> int:
        generation = self.last_generation(state) + 1
        state["last_generation"] = generation
        return generation

    def has_records(self, state: Mapping[str, object]) -> bool:
        teams = self._teams(state)
        for assistants in teams.values():
            if not isinstance(assistants, dict):
                raise self.error_class(self.malformed_state)
            for records in assistants.values():
                if not isinstance(records, dict):
                    raise self.error_class(self.malformed_state)
                if records:
                    return True
        return False

    def prune_empty_records(self, state: dict[str, object], team_id: str, assistant_id: str) -> None:
        teams = self._teams(state)
        assistants = teams.get(team_id)
        if not isinstance(assistants, dict):
            raise self.error_class(self.malformed_state)
        records = assistants.get(assistant_id)
        if records is not None and not isinstance(records, dict):
            raise self.error_class(self.malformed_state)
        if isinstance(records, dict) and not records:
            assistants.pop(assistant_id)
        if not assistants:
            teams.pop(team_id)

    def delete_assistant(self, state: dict[str, object], team_id: str, assistant_id: str) -> bool:
        teams = self._teams(state)
        assistants = teams.get(team_id)
        if assistants is None:
            return False
        if not isinstance(assistants, dict):
            raise self.error_class(self.malformed_state)
        removed = assistants.pop(assistant_id, None) is not None
        if removed and not assistants:
            teams.pop(team_id)
        return removed

    def delete_team(self, state: dict[str, object], team_id: str) -> bool:
        return self._teams(state).pop(team_id, None) is not None

    def require_private_directory(self, path: Path, label: str) -> None:
        """Create ``path`` when absent and require it to be a mode-0700 directory this process owns."""
        try:
            path.mkdir(mode=0o700, parents=True, exist_ok=True)
            metadata = path.stat(follow_symlinks=False)
        except OSError as exc:
            raise self.error_class(f"{label} directory is unavailable") from exc
        if (
            not stat.S_ISDIR(metadata.st_mode)
            or metadata.st_uid != os.geteuid()
            or stat.S_IMODE(metadata.st_mode) != 0o700
        ):
            raise self.error_class(f"{label} directory failed its ownership contract")

    def _teams(self, state: Mapping[str, object]) -> dict[str, object]:
        teams = state.get("teams")
        if not isinstance(teams, dict):
            raise self.error_class(self.malformed_state)
        return teams

