"""Edge coverage for durable Hosted Team cleanup authorization."""

from __future__ import annotations

import contextlib
import importlib.util
import json
import os
import sys
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
from hosted_assistant_fixture import hosted_lifecycle

cleanup = hosted_lifecycle.cleanup_state


def _record(**changes):
    base = cleanup.Record(
        version=cleanup.VERSION,
        team_id="team_1",
        owner="account_1",
        runtime_id="a" * 64,
        nonce="b" * 32,
    )
    return replace(base, **changes)


class HostedCleanupEdgeTests(unittest.TestCase):
    def test_import_rejects_nonpositive_record_capacity(self) -> None:
        module_name = "hosted_cleanup_invalid_capacity_test"
        spec = importlib.util.spec_from_file_location(module_name, Path(cleanup.__file__))
        self.assertIsNotNone(spec)
        module = importlib.util.module_from_spec(spec)
        self.assertIsNotNone(spec.loader)

        with (
            mock.patch.dict(os.environ, {"SHIMPZ_TEAM_CLEANUP_MAX_RECORDS": "0"}),
            mock.patch.dict(sys.modules, {module_name: module}),
            self.assertRaisesRegex(ValueError, "must be positive"),
        ):
            spec.loader.exec_module(module)

    def test_record_validation_rejects_every_invalid_field_family(self) -> None:
        class InvalidUnicodeOwner(str):
            def encode(self, *_args, **_kwargs):
                raise UnicodeEncodeError("utf-8", "x", 0, 1, "bad")

        invalid = (
            _record(version=True),
            _record(version=2),
            _record(team_id="TEAM"),
            _record(owner=123),
            _record(owner="x" * 257),
            _record(owner="bad\nowner"),
            _record(runtime_id=123),
            _record(runtime_id="z"),
            _record(nonce=123),
            _record(nonce="a"),
            _record(db_dropped=1),
        )

        for record in invalid:
            with self.subTest(record=record), self.assertRaises(cleanup.CleanupStateError):
                cleanup._validate_record(record)

        with self.assertRaisesRegex(cleanup.CleanupStateError, "invalid owner"):
            cleanup._validate_record(_record(owner=InvalidUnicodeOwner("owner")))

    def test_path_and_directory_validation_fail_closed(self) -> None:
        with self.assertRaisesRegex(cleanup.CleanupStateError, "invalid Team id"):
            cleanup._path("../team")

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            unsafe = root / "state"
            unsafe.write_text("not a directory")
            with (
                mock.patch.object(cleanup, "STATE_DIR", unsafe),
                self.assertRaisesRegex(cleanup.CleanupStateError, "unavailable"),
            ):
                cleanup._ensure_directory()

            state = root / "private"
            state.mkdir()
            with (
                mock.patch.object(cleanup, "STATE_DIR", state),
                mock.patch.object(cleanup.Path, "lstat", return_value=SimpleNamespace(st_mode=0o120777)),
                self.assertRaisesRegex(cleanup.CleanupStateError, "not a private directory"),
            ):
                cleanup._ensure_directory()

    def test_directory_fsync_wraps_open_and_fsync_failures(self) -> None:
        for target, side_effect in (("open", OSError("open")), ("fsync", OSError("sync"))):
            opened = mock.patch.object(cleanup.os, "open", return_value=7)
            open_context = opened if target == "fsync" else contextlib.nullcontext()
            with (
                open_context,
                mock.patch.object(cleanup.os, target, side_effect=side_effect),
                mock.patch.object(cleanup.os, "close") as close,
                self.assertRaisesRegex(cleanup.CleanupStateError, "could not be committed"),
            ):
                cleanup._fsync_directory()
            if target == "fsync":
                close.assert_called_once()

    def test_load_handles_missing_open_read_metadata_and_payload_failures(self) -> None:
        with tempfile.TemporaryDirectory() as directory, mock.patch.object(cleanup, "STATE_DIR", Path(directory)):
            self.assertIsNone(cleanup.load("team_1"))

            with (
                mock.patch.object(cleanup.os, "open", side_effect=PermissionError("denied")),
                self.assertRaisesRegex(cleanup.CleanupStateError, "could not be opened"),
            ):
                cleanup._load_unlocked("team_1")

            descriptor = os.open(Path(directory) / "raw", os.O_CREAT | os.O_RDWR, 0o600)
            with (
                mock.patch.object(cleanup.os, "open", return_value=descriptor),
                mock.patch.object(
                    cleanup.os,
                    "fstat",
                    return_value=SimpleNamespace(st_mode=0o100644, st_size=cleanup.MAX_RECORD_BYTES + 1),
                ),
                self.assertRaisesRegex(cleanup.CleanupStateError, "unsafe filesystem metadata"),
            ):
                cleanup._load_unlocked("team_1")

            descriptor = os.open(Path(directory) / "read", os.O_CREAT | os.O_RDWR, 0o600)
            with (
                mock.patch.object(cleanup.os, "open", return_value=descriptor),
                mock.patch.object(cleanup.os, "fstat", side_effect=OSError("read")),
                self.assertRaisesRegex(cleanup.CleanupStateError, "could not be read"),
            ):
                cleanup._load_unlocked("team_1")

            path = cleanup._path("team_1")
            for payload in (b"{", b"[]", json.dumps({"version": 1}).encode()):
                path.write_bytes(payload)
                path.chmod(0o600)
                with self.subTest(payload=payload), self.assertRaisesRegex(
                    cleanup.CleanupStateError,
                    "malformed",
                ):
                    cleanup._load_unlocked("team_1")

    def test_write_wraps_short_and_open_failures_and_cleans_temporary_file(self) -> None:
        with tempfile.TemporaryDirectory() as directory, mock.patch.object(cleanup, "STATE_DIR", Path(directory)):
            with (
                mock.patch.object(cleanup.os, "open", side_effect=OSError("open")),
                self.assertRaisesRegex(cleanup.CleanupStateError, "could not be committed"),
            ):
                cleanup._write_unlocked(_record())

            with (
                mock.patch.object(cleanup.os, "write", return_value=0),
                self.assertRaisesRegex(cleanup.CleanupStateError, "could not be committed"),
            ):
                cleanup._write_unlocked(_record())

            self.assertEqual(list(Path(directory).iterdir()), [])

    def test_begin_enforces_identity_inventory_and_capacity(self) -> None:
        with tempfile.TemporaryDirectory() as directory, mock.patch.object(cleanup, "STATE_DIR", Path(directory)):
            first = cleanup.begin("team_1", "account_1", "a" * 64)
            self.assertEqual(cleanup.begin("team_1", "account_1", "a" * 64), first)
            with self.assertRaisesRegex(cleanup.CleanupStateError, "identity does not match"):
                cleanup.begin("team_1", "account_2", "a" * 64)

            cleanup.finish(first)
            with (
                mock.patch.object(cleanup.Path, "glob", side_effect=OSError("inventory")),
                self.assertRaisesRegex(cleanup.CleanupStateError, "inventory is unavailable"),
            ):
                cleanup.begin("team_1", "account_1", "a" * 64)

            occupied = cleanup.STATE_DIR / "other.json"
            occupied.write_text("occupied")
            with (
                mock.patch.object(cleanup, "MAX_RECORDS", 1),
                self.assertRaisesRegex(cleanup.CleanupStateError, "capacity is exhausted"),
            ):
                cleanup.begin("team_1", "account_1", "a" * 64)

    def test_mark_finish_and_principal_authorization_are_record_bound(self) -> None:
        with tempfile.TemporaryDirectory() as directory, mock.patch.object(cleanup, "STATE_DIR", Path(directory)):
            record = cleanup.begin("team_1", "account_1", "a" * 64)
            with self.assertRaisesRegex(cleanup.CleanupStateError, "changed during teardown"):
                cleanup.mark_db_dropped(replace(record, owner="account_2"))

            completed = cleanup.mark_db_dropped(record)
            self.assertTrue(completed.db_dropped)
            with self.assertRaisesRegex(cleanup.CleanupStateError, "changed during teardown"):
                cleanup.finish(record)
            cleanup.finish(completed)
            cleanup.finish(completed)

            self.assertTrue(cleanup.principal_authorized(completed, ("supervisor", None)))
            self.assertTrue(cleanup.principal_authorized(completed, ("account", "account_1")))
            self.assertFalse(cleanup.principal_authorized(completed, ("account", "account_2")))

    def test_finish_wraps_unlink_failure(self) -> None:
        with tempfile.TemporaryDirectory() as directory, mock.patch.object(cleanup, "STATE_DIR", Path(directory)):
            record = cleanup.begin("team_1", "account_1", "a" * 64)
            with (
                mock.patch.object(Path, "unlink", side_effect=OSError("unlink")),
                self.assertRaisesRegex(cleanup.CleanupStateError, "could not be removed"),
            ):
                cleanup.finish(record)


if __name__ == "__main__":
    unittest.main()
