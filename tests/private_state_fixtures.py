"""Arrange and observe the storage/private_state boundary in tests: a fixed keyring, pinned nonces, modes, fsyncs."""

import contextlib
import os
import stat
import unittest
from collections.abc import Iterator
from pathlib import Path
from unittest import mock


def install_key(path: Path) -> None:
    """Install the fixed test keyring at ``path`` in its private directory, as a store's own keyring would be."""
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    path.write_bytes(bytes(range(32)))
    path.chmod(0o600)


@contextlib.contextmanager
def pinned_nonces(*nonces: bytes) -> Iterator[None]:
    """Seal with exactly these nonces, in order, so sealed bytes are reproducible."""
    values = iter(nonces)
    with mock.patch("os.urandom", side_effect=lambda _size: next(values)):
        yield


def assert_private_modes(case: unittest.TestCase, state_path: Path, key_path: Path) -> None:
    """The sealed state and its keyring are owner-only files, each in an owner-only directory."""
    case.assertEqual(stat.S_IMODE(state_path.stat().st_mode), 0o600)
    case.assertEqual(stat.S_IMODE(key_path.stat().st_mode), 0o600)
    case.assertEqual(stat.S_IMODE(state_path.parent.stat().st_mode), 0o700)
    case.assertEqual(stat.S_IMODE(key_path.parent.stat().st_mode), 0o700)


@contextlib.contextmanager
def observed_fsyncs() -> Iterator[list[str]]:
    """Record each real fsync as ``"file"`` or ``"directory"``, in order."""
    real_fsync = os.fsync
    synced: list[str] = []

    def observe(descriptor: int) -> None:
        real_fsync(descriptor)
        synced.append("directory" if stat.S_ISDIR(os.fstat(descriptor).st_mode) else "file")

    with mock.patch.object(os, "fsync", side_effect=observe):
        yield synced
