"""Cover encrypted private-state plumbing independently of OAuth record semantics."""

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

        read_only = OSError(30, "Read-only file system")
        real_open = os.open

        def create_denied(path, flags, *args, **kwargs):
            if flags & os.O_CREAT:
                raise read_only
            return real_open(path, flags, *args, **kwargs)

        with (
            mock.patch.object(private_state.os, "open", side_effect=create_denied),
            mock.patch.object(private_state.os, "unlink", side_effect=read_only),
            self.assertRaisesRegex(RuntimeError, "could not be persisted"),
        ):
            self.state.key(self.root / "private" / "new-key", "key", allow_create=True)

        with self.assertRaisesRegex(RuntimeError, "unavailable"):
            self.state.key(self.root / "missing-key", "key")
        invalid = self.root / "invalid-key"
        invalid.write_bytes(b"short")
        invalid.chmod(0o600)
        with self.assertRaisesRegex(RuntimeError, "invalid"):
            self.state.key(invalid, "key")

    def test_replace_durably_fixes_mode_then_commits_the_directory_entry(self) -> None:
        path = self.root / "record"
        path.write_bytes(b"old")
        real_fsync = os.fsync
        synced: list[tuple[bool, bytes]] = []

        def observe(descriptor: int) -> None:
            real_fsync(descriptor)
            synced.append((stat.S_ISDIR(os.fstat(descriptor).st_mode), path.read_bytes()))

        with mock.patch.object(private_state.os, "fsync", side_effect=observe):
            private_state.replace_durably(path, b"new", mode=0o640, group=os.getgid())
        self.assertEqual(synced, [(False, b"old"), (True, b"new")])
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o640)

        def fail_directory(descriptor: int) -> None:
            if stat.S_ISDIR(os.fstat(descriptor).st_mode):
                raise OSError("directory sync")
            real_fsync(descriptor)

        with (
            mock.patch.object(private_state.os, "fsync", side_effect=fail_directory),
            self.assertRaisesRegex(OSError, "directory sync"),
        ):
            private_state.replace_durably(path, b"next")
        self.assertEqual([entry.name for entry in self.root.iterdir()], ["record"])

    def test_replace_stays_anchored_to_the_opened_directory(self) -> None:
        opened = self.root / "opened"
        opened.mkdir()
        directory = os.open(opened, os.O_RDONLY | os.O_DIRECTORY)
        self.addCleanup(os.close, directory)
        opened.rename(self.root / "moved")
        opened.mkdir()

        private_state.replace_in_directory(directory, "record", b"anchored")

        self.assertEqual((self.root / "moved" / "record").read_bytes(), b"anchored")
        self.assertEqual(list(opened.iterdir()), [])

    def test_replace_rejects_a_symbolic_link_parent_before_any_write(self) -> None:
        target = self.root / "target"
        target.mkdir()
        link = self.root / "link"
        link.symlink_to(target, target_is_directory=True)

        with self.assertRaises(OSError):
            private_state.replace_durably(link / "record", b"value")
        self.assertEqual(list(target.iterdir()), [])

    def test_directory_commit_failures_propagate_and_close_the_descriptor(self) -> None:
        with self.assertRaises(FileNotFoundError):
            private_state.fsync_directory(self.root / "missing")
        link = self.root / "link"
        link.symlink_to(self.root, target_is_directory=True)
        with self.assertRaises(OSError):
            private_state.fsync_directory(link)

        descriptors = len(list(Path("/proc/self/fd").iterdir()))
        with (
            mock.patch.object(private_state.os, "fsync", side_effect=OSError("sync")),
            self.assertRaisesRegex(OSError, "sync"),
        ):
            private_state.fsync_directory(self.root)
        self.assertEqual(len(list(Path("/proc/self/fd").iterdir())), descriptors)

    def test_replace_completes_partial_writes(self) -> None:
        path = self.root / "record"
        real_write = os.write

        def one_byte(descriptor: int, view: memoryview) -> int:
            return real_write(descriptor, view[:1])

        with mock.patch.object(private_state.os, "write", side_effect=one_byte):
            private_state.replace_durably(path, b"complete")
        self.assertEqual(path.read_bytes(), b"complete")

    def test_every_replace_step_failure_propagates_and_leaves_no_temporary(self) -> None:
        path = self.root / "record"
        path.write_bytes(b"old")
        descriptors = len(list(Path("/proc/self/fd").iterdir()))
        for step in ("fchown", "fchmod", "fsync", "rename"):
            with (
                self.subTest(step=step),
                mock.patch.object(private_state.os, step, side_effect=OSError(step)),
                self.assertRaisesRegex(OSError, step),
            ):
                private_state.replace_durably(path, b"new", group=os.getgid())
            self.assertEqual([entry.name for entry in self.root.iterdir()], ["record"])
            self.assertEqual(path.read_bytes(), b"old")
        self.assertEqual(len(list(Path("/proc/self/fd").iterdir())), descriptors)

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
            self.state.require_private_directory(path, "state")

        unsafe = self.root / "unsafe-parent"
        unsafe.mkdir(mode=0o755)
        with self.assertRaisesRegex(RuntimeError, "ownership contract"):
            self.state.require_private_directory(unsafe, "state")
        with self.assertRaisesRegex(RuntimeError, "malformed"):
            self.state._teams({"teams": []})

    def test_an_identified_envelope_opens_only_under_the_key_it_names(self) -> None:
        state = private_state.PrivateState(RuntimeError, "malformed state", "malformed envelope", 4096)
        key, other = b"k" * 32, b"o" * 32
        envelope = private_state.seal_identified(key, b"secret", b"aad")
        state.check_identified_envelope(envelope, 64, "malformed")
        self.assertEqual(envelope["key_id"], private_state.key_id(key))
        self.assertNotEqual(private_state.key_id(key), private_state.key_id(other))
        self.assertEqual(state.open_identified_envelope(key, envelope, b"aad", 64, "refused"), b"secret")
        # A record sealed under another key is refused by its key_id before any decryption is attempted.
        with (
            mock.patch.object(private_state.AESGCM, "decrypt") as decrypt,
            self.assertRaisesRegex(RuntimeError, "refused"),
        ):
            state.open_identified_envelope(other, envelope, b"aad", 64, "refused")
        decrypt.assert_not_called()
        unidentified = {name: value for name, value in envelope.items() if name != "key_id"}
        for value in (unidentified, {**envelope, "key_id": 1}, {**envelope, "key_id": "F" * 32}, []):
            with self.subTest(value=value), self.assertRaisesRegex(RuntimeError, "malformed"):
                state.check_identified_envelope(value, 64, "malformed")


class JsonRecordTests(unittest.TestCase):
    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name, "records", "record.json")

    def test_a_record_round_trips_as_canonical_owner_only_json(self) -> None:
        self.assertIsNone(private_state.read_json_record(self.path, 64))
        private_state.write_json_record(self.path, {"name": "Équipe", "count": 1}, 64)
        self.assertEqual(self.path.read_bytes(), '{"count":1,"name":"Équipe"}'.encode())
        self.assertEqual(stat.S_IMODE(self.path.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(self.path.parent.stat().st_mode), 0o700)
        self.assertEqual(private_state.read_json_record(self.path, 64), {"count": 1, "name": "Équipe"})

    def test_an_oversized_record_is_neither_written_nor_read(self) -> None:
        with self.assertRaisesRegex(ValueError, "byte limit"):
            private_state.write_json_record(self.path, {"name": "x" * 64}, 16)
        self.assertFalse(self.path.parent.exists())
        self.path.parent.mkdir()
        self.path.write_bytes(b'"' + b"x" * 16 + b'"')
        with self.assertRaisesRegex(ValueError, "byte limit"):
            private_state.read_json_record(self.path, 16)

    def test_only_a_strict_json_document_in_a_regular_file_is_read(self) -> None:
        self.path.parent.mkdir()
        for raw in (b"", b"\xff", b'{"a":1,"a":2}', b"NaN", b"1e999", b"[" * 100_000):
            with self.subTest(raw=raw[:16]):
                self.path.write_bytes(raw)
                with self.assertRaises(ValueError):
                    private_state.read_json_record(self.path, 200_000)
        self.path.unlink()
        self.path.mkdir()
        with self.assertRaises(OSError):
            private_state.read_json_record(self.path, 64)
        self.path.rmdir()
        self.path.symlink_to(self.path.with_name("target.json"))
        self.path.with_name("target.json").write_bytes(b"{}")
        with self.assertRaises(OSError):
            private_state.read_json_record(self.path, 64)


class CustodyPredicateTests(unittest.TestCase):
    @staticmethod
    def metadata(kind: int, mode: int, *, uid: int = 1, gid: int = 2, nlink: int = 1, size: int = 5):
        return SimpleNamespace(st_mode=kind | mode, st_uid=uid, st_gid=gid, st_nlink=nlink, st_size=size)

    def test_file_custody_checks_every_stated_field_and_skips_only_none(self) -> None:
        holds = self.metadata(stat.S_IFREG, 0o600)
        self.assertTrue(private_state.file_custody_holds(holds, uid=1, gid=2, mode=0o600, size=(5, 5)))
        self.assertTrue(private_state.file_custody_holds(holds, uid=None, gid=None, mode=None, size=None))
        self.assertTrue(
            private_state.file_custody_holds(holds, uid=1, gid=None, mode=private_state.OWNER_ONLY, size=None)
        )
        for metadata, fields in (
            (self.metadata(stat.S_IFDIR, 0o600), {}),
            (self.metadata(stat.S_IFREG, 0o600, nlink=2), {}),
            (holds, {"uid": 3}),
            (holds, {"gid": 3}),
            (holds, {"mode": 0o640}),
            (self.metadata(stat.S_IFREG, 0o640), {"mode": private_state.OWNER_ONLY}),
            (holds, {"size": (6, 10)}),
            (holds, {"size": (0, 4)}),
        ):
            arguments = {"uid": 1, "gid": 2, "mode": 0o600, "size": (0, 10), **fields}
            with self.subTest(fields=fields):
                self.assertFalse(private_state.file_custody_holds(metadata, **arguments))

    def test_directory_custody_checks_kind_owner_group_and_mode(self) -> None:
        holds = self.metadata(stat.S_IFDIR, 0o2770)
        self.assertTrue(private_state.directory_custody_holds(holds, uid=1, gid=2, mode=0o2770))
        self.assertTrue(private_state.directory_custody_holds(holds, uid=None, gid=None, mode=None))
        for metadata, fields in (
            (self.metadata(stat.S_IFREG, 0o2770), {}),
            (holds, {"uid": 3}),
            (holds, {"gid": 3}),
            (holds, {"mode": 0o770}),
            (holds, {"mode": private_state.OWNER_ONLY}),
        ):
            arguments = {"uid": 1, "gid": 2, "mode": 0o2770, **fields}
            with self.subTest(fields=fields):
                self.assertFalse(private_state.directory_custody_holds(metadata, **arguments))


if __name__ == "__main__":
    unittest.main()
