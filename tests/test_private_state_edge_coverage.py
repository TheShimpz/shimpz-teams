"""Cover encrypted private-state plumbing independently of OAuth record semantics."""

from __future__ import annotations

import base64
import os
import stat
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from storage import private_state


class PrivateStateEdgeCoverageTests(unittest.TestCase):
    def setUp(self) -> None:
        self.state = private_state.PrivateState(RuntimeError, "malformed state", "malformed envelope", 16)
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)

    def test_decode_part_rejects_type_encoding_and_length_bounds(self) -> None:
        for value, options in (
            (None, {}),
            ("x" * 17, {}),
            ("!", {}),
            (base64.b64encode(b"a").decode(), {"expected": 2}),
            (base64.b64encode(b"a").decode(), {"minimum": 2}),
            (base64.b64encode(b"ab").decode(), {"maximum": 1}),
        ):
            with self.subTest(value=value, options=options), self.assertRaisesRegex(RuntimeError, "envelope"):
                self.state.decode_part(value, **options)

    def test_private_file_missing_cache_ownership_change_and_size_edges(self) -> None:
        missing = self.root / "missing"
        self.assertEqual(
            self.state.read_private_file_if_changed(missing, 10, "state", None, cache_initialized=False),
            private_state.PrivateFileRead(None, None, False),
        )
        self.assertTrue(
            self.state.read_private_file_if_changed(missing, 10, "state", None, cache_initialized=True).unchanged
        )
        with (
            mock.patch.object(private_state.os, "open", side_effect=OSError("denied")),
            self.assertRaisesRegex(RuntimeError, "unavailable"),
        ):
            self.state.read_private_file(missing, 10, "state")

        unsafe = self.root / "unsafe"
        unsafe.mkdir()
        with self.assertRaisesRegex(RuntimeError, "ownership contract"):
            self.state.read_private_file(unsafe, 10, "state")

        path = self.root / "state"
        path.write_bytes(b"value")
        path.chmod(0o600)
        first = self.state.read_private_file_if_changed(path, 10, "state", None, cache_initialized=False)
        self.assertTrue(
            self.state.read_private_file_if_changed(
                path,
                10,
                "state",
                first.identity,
                cache_initialized=True,
            ).unchanged
        )

        metadata = SimpleNamespace(
            st_mode=stat.S_IFREG | 0o600,
            st_uid=os.geteuid(),
            st_nlink=1,
            st_size=1,
            st_dev=1,
            st_ino=1,
            st_mtime_ns=1,
            st_ctime_ns=1,
        )
        with (
            mock.patch.object(private_state.os, "open", return_value=3),
            mock.patch.object(private_state.os, "fstat", return_value=metadata),
            mock.patch.object(private_state.os, "read", return_value=b"xx"),
            mock.patch.object(private_state.os, "close"),
            self.assertRaisesRegex(RuntimeError, "fixed byte limit"),
        ):
            self.state.read_private_file(path, 1, "state")

    def test_atomic_write_short_write_and_key_edges_fail_closed(self) -> None:
        path = self.root / "private" / "state"
        with (
            mock.patch.object(private_state.os, "write", return_value=0),
            self.assertRaisesRegex(RuntimeError, "could not be persisted"),
        ):
            self.state.atomic_write(path, b"payload", "state")

        with self.assertRaisesRegex(RuntimeError, "unavailable"):
            self.state.key(self.root / "missing-key", "key")
        invalid = self.root / "invalid-key"
        invalid.write_bytes(b"short")
        invalid.chmod(0o600)
        with self.assertRaisesRegex(RuntimeError, "invalid"):
            self.state.key(invalid, "key")

    def test_record_shapes_has_records_prune_and_delete_edges(self) -> None:
        state = private_state.empty_state()
        self.assertEqual(self.state.records(state, "team", "assistant", create=False), {})
        records = self.state.records(state, "team", "assistant", create=True)
        self.assertFalse(self.state.has_records(state))
        records["record"] = {}
        self.assertTrue(self.state.has_records(state))

        for malformed in (
            {"teams": {"team": []}},
            {"teams": {"team": {"assistant": []}}},
        ):
            with self.subTest(malformed=malformed), self.assertRaisesRegex(RuntimeError, "malformed"):
                self.state.records(malformed, "team", "assistant", create=True)
            with self.assertRaisesRegex(RuntimeError, "malformed"):
                self.state.has_records(malformed)

        with self.assertRaisesRegex(RuntimeError, "malformed"):
            self.state.prune_empty_records({"teams": {}}, "missing", "assistant")
        with self.assertRaisesRegex(RuntimeError, "malformed"):
            self.state.prune_empty_records({"teams": {"team": {"assistant": []}}}, "team", "assistant")

        empty = {"teams": {"team": {"assistant": {}}}}
        self.state.prune_empty_records(empty, "team", "assistant")
        self.assertEqual(empty, {"teams": {}})

        self.assertFalse(self.state.delete_assistant(private_state.empty_state(), "team", "assistant"))
        remaining = {"teams": {"team": {"first": {}, "second": {}}}}
        self.assertTrue(self.state.delete_assistant(remaining, "team", "first"))
        self.assertEqual(remaining, {"teams": {"team": {"second": {}}}})
        with self.assertRaisesRegex(RuntimeError, "malformed"):
            self.state.delete_assistant({"teams": {"team": []}}, "team", "assistant")

    def test_private_parent_and_root_state_shapes_fail_closed(self) -> None:
        path = mock.Mock()
        path.mkdir.side_effect = OSError("denied")
        with self.assertRaisesRegex(RuntimeError, "directory is unavailable"):
            self.state._require_private_parent(path, "state")

        unsafe = self.root / "unsafe-parent"
        unsafe.mkdir(mode=0o755)
        with self.assertRaisesRegex(RuntimeError, "ownership contract"):
            self.state._require_private_parent(unsafe, "state")
        with self.assertRaisesRegex(RuntimeError, "malformed"):
            self.state._teams({"teams": []})


if __name__ == "__main__":
    unittest.main()
