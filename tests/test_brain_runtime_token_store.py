from __future__ import annotations

import os
import stat
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from inference import token as brain_runtime_token_store


class BrainRuntimeTokenStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name) / "runtime-token"
        self.path = self.root / "token"
        self.group_id = os.getgid()

    def test_create_uses_a_strong_token_and_exact_shared_group_permissions(self) -> None:
        token = brain_runtime_token_store.ensure(self.path, group_id=self.group_id)

        self.assertEqual(len(bytes.fromhex(token)), 32)
        self.assertEqual(stat.S_IMODE(self.root.stat().st_mode), 0o750)
        self.assertEqual(stat.S_IMODE(self.path.stat().st_mode), 0o440)
        self.assertEqual(self.root.stat().st_gid, self.group_id)
        self.assertEqual(self.path.stat().st_gid, self.group_id)
        self.assertEqual(list(self.root.glob(".*.tmp")), [])

    def test_existing_safe_token_is_reused_without_replacement(self) -> None:
        first = brain_runtime_token_store.ensure(self.path, group_id=self.group_id)
        inode = self.path.stat().st_ino

        second = brain_runtime_token_store.ensure(self.path, group_id=self.group_id)

        self.assertEqual(second, first)
        self.assertEqual(self.path.stat().st_ino, inode)

    def test_symlink_and_insecure_existing_file_fail_closed(self) -> None:
        self.root.mkdir(mode=0o750)
        victim = Path(self.directory.name) / "victim"
        victim.write_text("do-not-read", encoding="ascii")
        self.path.symlink_to(victim)
        with self.assertRaises(brain_runtime_token_store.RuntimeTokenError):
            brain_runtime_token_store.ensure(self.path, group_id=self.group_id)
        self.assertEqual(victim.read_text(encoding="ascii"), "do-not-read")

        self.path.unlink()
        self.path.write_text("a" * 64, encoding="ascii")
        self.path.chmod(0o640)
        with self.assertRaises(brain_runtime_token_store.RuntimeTokenError):
            brain_runtime_token_store.ensure(self.path, group_id=self.group_id)

    def test_unsafe_directory_and_directory_creation_errors_fail_closed(self) -> None:
        metadata = SimpleNamespace(st_mode=stat.S_IFREG | 0o750, st_uid=os.geteuid(), st_gid=self.group_id)
        with self.assertRaisesRegex(brain_runtime_token_store.RuntimeTokenError, "unsafe metadata"):
            brain_runtime_token_store._check_directory(metadata, self.group_id)

        path = mock.Mock()
        path.lstat.side_effect = (FileNotFoundError(), OSError("unavailable"))
        path.mkdir.side_effect = OSError("read-only")
        with self.assertRaisesRegex(brain_runtime_token_store.RuntimeTokenError, "could not be created"):
            brain_runtime_token_store._prepare_directory(path, self.group_id)

    def test_checked_read_rejects_open_short_encoding_and_hex_length_failures(self) -> None:
        with (
            mock.patch.object(os, "open", side_effect=OSError("denied")),
            self.assertRaisesRegex(brain_runtime_token_store.RuntimeTokenError, "unavailable or unsafe"),
        ):
            brain_runtime_token_store._read_checked(1, "token", self.group_id)

        safe = SimpleNamespace(
            st_mode=stat.S_IFREG | 0o440,
            st_nlink=1,
            st_uid=os.geteuid(),
            st_gid=self.group_id,
            st_size=brain_runtime_token_store.TOKEN_BYTES * 2,
        )
        for raw in (b"short", b"g" * 64, b" " * 64):
            with (
                self.subTest(raw=raw[:8]),
                mock.patch.object(os, "open", return_value=3),
                mock.patch.object(os, "fstat", return_value=safe),
                mock.patch.object(os, "read", return_value=raw),
                mock.patch.object(os, "close"),
                self.assertRaisesRegex(brain_runtime_token_store.RuntimeTokenError, "token is invalid"),
            ):
                brain_runtime_token_store._read_checked(1, "token", self.group_id)

    def test_create_and_directory_open_errors_close_partial_resources(self) -> None:
        with (
            mock.patch.object(os, "open", return_value=9),
            mock.patch.object(os, "write", side_effect=OSError("full")),
            mock.patch.object(os, "close") as close,
            mock.patch.object(os, "unlink"),
            self.assertRaisesRegex(brain_runtime_token_store.RuntimeTokenError, "could not be created"),
        ):
            brain_runtime_token_store._create(1, "token", self.group_id)
        close.assert_called_once_with(9)

        self.root.mkdir(mode=0o750)
        with (
            mock.patch.object(os, "open", side_effect=OSError("denied")),
            self.assertRaisesRegex(brain_runtime_token_store.RuntimeTokenError, "directory is unavailable"),
        ):
            brain_runtime_token_store.ensure(self.path, group_id=self.group_id)


if __name__ == "__main__":
    unittest.main()
