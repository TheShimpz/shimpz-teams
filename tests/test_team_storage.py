from __future__ import annotations

import concurrent.futures
import sqlite3
import stat
import sys
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest import mock

TEAM = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(TEAM))

from protocol.http.v1 import payload as http_payload
from storage import files as team_storage


class TeamStorageTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary_directory.name) / "teams"

    def tearDown(self) -> None:
        self.temporary_directory.cleanup()

    def test_round_trip_is_opaque_and_isolated_by_team(self) -> None:
        storage = team_storage.TeamStorage(self.root, limit_bytes=128)
        first = storage.put("alpha", "brief.txt", b"confidential", "text/plain")
        second = storage.put("beta", "brief.txt", b"different", "text/plain")

        metadata, content = storage.get("alpha", first["id"])
        self.assertEqual(content, b"confidential")
        self.assertEqual(metadata["name"], "brief.txt")
        self.assertNotEqual(first["id"], second["id"])
        with self.assertRaises(team_storage.StorageNotFoundError):
            storage.get("beta", first["id"])
        selected = storage.metadata("alpha", [first["id"]])[0]
        self.assertEqual(
            set(selected),
            {"id", "name", "media_type", "size", "sha256"},
        )
        self.assertEqual(selected["name"], "brief.txt")
        self.assertEqual(selected["sha256"], metadata["sha256"])
        with self.assertRaises(team_storage.StorageNotFoundError):
            storage.metadata("beta", [first["id"]])

        alpha_directory = self.root / "alpha"
        self.assertEqual({path.name for path in alpha_directory.iterdir()}, {"files.sqlite3"})
        self.assertFalse((alpha_directory / "brief.txt").exists())
        self.assertEqual(stat.S_IMODE(alpha_directory.stat().st_mode), 0o700)
        self.assertEqual(stat.S_IMODE((alpha_directory / "files.sqlite3").stat().st_mode), 0o600)

    def test_an_upload_answer_is_the_exact_protocol_upload_shape(self) -> None:
        storage = team_storage.TeamStorage(self.root, limit_bytes=128)
        stored = storage.put("alpha", "brief.txt", b"confidential", "text/plain")
        projected = http_payload.project_storage_response(
            {"team_id": "alpha", "file": stored}, kind="upload", expected_team_id="alpha", include_team_id=True
        )
        self.assertIsNotNone(projected)
        self.assertEqual(projected["file"]["created_at"], storage.list("alpha")["files"][0]["created_at"])

    def test_exact_content_quota_is_transactional(self) -> None:
        storage = team_storage.TeamStorage(self.root, limit_bytes=10)
        storage.put("alpha", "first.bin", b"1234")
        exact = storage.put("alpha", "second.bin", b"567890")
        self.assertEqual(exact["used_bytes"], 10)
        self.assertEqual(exact["remaining_bytes"], 0)

        with self.assertRaises(team_storage.StorageQuotaError):
            storage.put("alpha", "overflow.bin", b"x")
        listing = storage.list("alpha")
        self.assertEqual(listing["used_bytes"], 10)
        self.assertEqual(len(listing["files"]), 2)

    def test_quota_resolver_is_team_scoped_and_server_trusted(self) -> None:
        limits = {"alpha": 4, "beta": 8}
        storage = team_storage.TeamStorage(self.root, quota_for=limits.__getitem__)
        alpha = storage.put("alpha", "alpha.bin", b"1234")
        beta = storage.put("beta", "beta.bin", b"12345678")
        self.assertEqual((alpha["limit_bytes"], beta["limit_bytes"]), (4, 8))
        with self.assertRaises(team_storage.StorageQuotaError):
            storage.put("alpha", "overflow.bin", b"x")

    def test_plan_downgrade_blocks_writes_but_keeps_cleanup_available(self) -> None:
        limits = {"alpha": 8}
        storage = team_storage.TeamStorage(self.root, quota_for=limits.__getitem__)
        stored = storage.put("alpha", "before-downgrade.bin", b"12345678")

        limits["alpha"] = 4
        listing = storage.list("alpha")
        self.assertEqual(listing["used_bytes"], 8)
        self.assertEqual(listing["remaining_bytes"], 0)
        with self.assertRaises(team_storage.StorageQuotaError):
            storage.put("alpha", "blocked.bin", b"x")

        self.assertTrue(storage.delete("alpha", stored["id"])["deleted"])
        replacement = storage.put("alpha", "within-new-plan.bin", b"1234")
        self.assertEqual(replacement["remaining_bytes"], 0)

    def test_concurrent_writes_cannot_overbook_quota(self) -> None:
        storage = team_storage.TeamStorage(self.root, limit_bytes=10)

        def write(index: int) -> bool:
            try:
                storage.put("alpha", f"{index}.bin", b"123456")
            except team_storage.StorageQuotaError:
                return False
            return True

        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(write, range(2)))

        self.assertEqual(sorted(results), [False, True])
        self.assertEqual(storage.list("alpha")["used_bytes"], 6)

    def test_delete_releases_logical_quota_and_destroy_is_scoped(self) -> None:
        storage = team_storage.TeamStorage(self.root, limit_bytes=8)
        alpha = storage.put("alpha", "alpha.bin", b"12345678")
        beta = storage.put("beta", "beta.bin", b"abcdefgh")

        deleted = storage.delete("alpha", alpha["id"])
        self.assertTrue(deleted["deleted"])
        self.assertEqual(deleted["used_bytes"], 0)
        storage.put("alpha", "replacement.bin", b"87654321")

        self.assertTrue(storage.destroy("alpha"))
        self.assertFalse(storage.destroy("alpha"))
        self.assertEqual(storage.get("beta", beta["id"])[1], b"abcdefgh")

        storage.put("orphan", "orphan.bin", b"x")
        self.assertEqual(storage.destroy_all(), 2)
        self.assertEqual(storage.list("beta")["files"], [])
        self.assertEqual(storage.list("orphan")["files"], [])

    def test_file_count_and_metadata_are_bounded(self) -> None:
        storage = team_storage.TeamStorage(
            self.root,
            limit_bytes=team_storage.MAX_FILES + 1,
        )
        for index in range(team_storage.MAX_FILES):
            storage.put("alpha", f"{index}.txt", b"x", "text/plain")
        with self.assertRaises(team_storage.StorageQuotaError):
            storage.put("alpha", "one-too-many.txt", b"x")

        invalid_names = ("", " ../secret", "../secret", "nested/file", "line\nfeed")
        for name in invalid_names:
            with self.subTest(name=name), self.assertRaises(team_storage.StorageError):
                storage.put("beta", name, b"x")
        with self.assertRaises(team_storage.StorageError):
            storage.put("beta", "safe.txt", b"x", "text/plain; charset=utf-8")

    def test_selected_metadata_reuses_one_bounded_reader(self) -> None:
        storage = team_storage.TeamStorage(self.root, limit_bytes=128)
        selected = storage.put("alpha", "selected.txt", b"selected", "text/plain")
        storage.put("alpha", "other.txt", b"other", "text/plain")
        storage.list = mock.Mock(side_effect=AssertionError("metadata must not scan the full inventory"))
        statements: list[str] = []

        with (
            mock.patch.object(storage, "_connect", wraps=storage._connect) as connect,
            storage.metadata_connection("alpha", [selected["id"]]) as reader,
        ):
            self.assertIsNotNone(reader)
            reader.connection.set_trace_callback(statements.append)
            first = storage.metadata("alpha", [selected["id"]], reader)
            second = storage.metadata("alpha", [selected["id"]], reader)
            with self.assertRaises(team_storage.StorageError):
                storage.metadata("beta", [selected["id"]], reader)

        self.assertEqual(first, second)
        self.assertEqual(first[0]["name"], "selected.txt")
        connect.assert_called_once()
        selects = [statement for statement in statements if statement.startswith("SELECT ")]
        self.assertEqual(len(selects), 2)
        self.assertTrue(all(" WHERE id IN (" in statement for statement in selects))
        storage.list.assert_not_called()

    def test_database_page_ceiling_and_integrity_check_fail_closed(self) -> None:
        storage = team_storage.TeamStorage(self.root, limit_bytes=64)
        stored = storage.put("alpha", "safe.bin", b"safe")
        with closing(storage._connect("alpha", create=False)) as connection:
            page_size = int(connection.execute("PRAGMA page_size").fetchone()[0])
            expected = (64 + team_storage.DATABASE_HEADROOM_BYTES + page_size - 1) // page_size
            self.assertEqual(int(connection.execute("PRAGMA max_page_count").fetchone()[0]), expected)
            self.assertEqual(int(connection.execute("PRAGMA user_version").fetchone()[0]), team_storage.SCHEMA_VERSION)
            connection.execute("UPDATE files SET content=? WHERE id=?", (b"evil", stored["id"]))

        with self.assertRaises(team_storage.StorageError):
            storage.get("alpha", stored["id"])

    def test_unsafe_storage_shapes_are_rejected(self) -> None:
        self.root.mkdir(mode=0o700)
        team = self.root / "alpha"
        team.mkdir(mode=0o700)
        target = self.root / "target"
        target.write_bytes(b"outside")
        (team / "files.sqlite3").symlink_to(target)

        storage = team_storage.TeamStorage(self.root, limit_bytes=64)
        with self.assertRaises(team_storage.StorageError):
            storage.put("alpha", "safe.bin", b"safe")
        self.assertEqual(target.read_bytes(), b"outside")

    def test_identifiers_limits_and_file_inputs_fail_closed(self) -> None:
        with self.assertRaises(team_storage.StorageError):
            team_storage.TeamStorage(self.root, limit_bytes=True)
        with self.assertRaises(team_storage.StorageError):
            team_storage.TeamStorage(self.root, limit_bytes=0)

        storage = team_storage.TeamStorage(self.root, limit_bytes=4)
        with self.assertRaises(team_storage.StorageError):
            storage.list("INVALID")
        with self.assertRaises(team_storage.StorageNotFoundError):
            storage.get("alpha", "not-an-id")
        with self.assertRaises(team_storage.StorageInputError):
            storage.put("alpha", "x" * (team_storage.MAX_FILENAME_BYTES + 1), b"x")
        with self.assertRaises(team_storage.StorageInputError):
            storage.put("alpha", "empty.bin", b"")
        with self.assertRaises(team_storage.StorageInputError):
            storage.put("alpha", "not-bytes.bin", bytearray(b"x"))
        with self.assertRaises(team_storage.StorageQuotaError):
            storage.put("alpha", "large.bin", b"12345")

    def test_storage_paths_reject_unavailable_and_unsafe_directories(self) -> None:
        with (
            mock.patch.object(Path, "mkdir", side_effect=OSError("denied")),
            self.assertRaisesRegex(team_storage.StorageError, "root is unavailable"),
        ):
            team_storage.TeamStorage(self.root)

        self.root.mkdir(mode=0o700)
        self.root.chmod(stat.S_IMODE(self.root.stat().st_mode) | stat.S_IRGRP)
        with self.assertRaisesRegex(team_storage.StorageError, "unsafe ownership or permissions"):
            team_storage.TeamStorage(self.root)

        self.root.chmod(0o700)
        storage = team_storage.TeamStorage(self.root)
        team = self.root / "alpha"
        team.mkdir(mode=0o700)
        team.chmod(stat.S_IMODE(team.stat().st_mode) | stat.S_IRGRP)
        with self.assertRaisesRegex(team_storage.StorageError, "Team storage has unsafe"):
            storage.list("alpha")
        with self.assertRaisesRegex(team_storage.StorageError, "Team storage has unsafe"):
            storage.destroy("alpha")

        team.chmod(0o700)
        self.assertFalse(storage._database_path("alpha", create=False).exists())
        with self.assertRaises(team_storage.StorageNotFoundError):
            storage._connect("alpha", create=False)

    def test_connection_setup_fails_closed_and_closes_the_database(self) -> None:
        storage = team_storage.TeamStorage(self.root, limit_bytes=64)
        path = self.root / "alpha" / "files.sqlite3"
        path.parent.mkdir(mode=0o700)
        path.touch(mode=0o600)

        failed_connection = mock.Mock()
        failed_connection.execute.side_effect = RuntimeError("pragma failed")
        with (
            mock.patch.object(sqlite3, "connect", return_value=failed_connection),
            self.assertRaisesRegex(RuntimeError, "pragma failed"),
        ):
            storage._connect("alpha", create=False)
        failed_connection.close.assert_called_once_with()

        mismatched_connection = mock.Mock()

        def execute(statement: str) -> mock.Mock:
            result = mock.Mock()
            if statement == "PRAGMA user_version":
                result.fetchone.return_value = (team_storage.SCHEMA_VERSION,)
            elif statement == "PRAGMA page_size":
                result.fetchone.return_value = (4096,)
            elif statement == "PRAGMA page_count":
                result.fetchone.return_value = (0,)
            elif statement.startswith("PRAGMA max_page_count="):
                result.fetchone.return_value = (1,)
            return result

        mismatched_connection.execute.side_effect = execute
        with (
            mock.patch.object(sqlite3, "connect", return_value=mismatched_connection),
            self.assertRaisesRegex(team_storage.StorageError, "page limit could not be applied"),
        ):
            storage._connect("alpha", create=False)
        mismatched_connection.close.assert_called_once_with()

    def test_database_errors_are_mapped_without_leaking_sqlite_failures(self) -> None:
        storage = team_storage.TeamStorage(self.root, limit_bytes=64)
        for detail, expected in (
            ("database or disk is full", team_storage.StorageQuotaError),
            ("malformed database", team_storage.StorageError),
        ):
            with self.subTest(detail=detail):
                connection = mock.Mock()
                connection.execute.side_effect = sqlite3.DatabaseError(detail)
                with (
                    mock.patch.object(storage, "_connect", return_value=connection),
                    self.assertRaises(expected),
                ):
                    storage.put("alpha", "safe.bin", b"safe")
                connection.close.assert_called_once_with()

    def test_missing_storage_metadata_bounds_and_delete_rollback(self) -> None:
        storage = team_storage.TeamStorage(self.root, limit_bytes=64)
        missing_id = "0" * 32
        with self.assertRaises(team_storage.StorageNotFoundError):
            storage.get("alpha", missing_id)
        self.assertEqual(storage.metadata("alpha", []), [])
        with storage.metadata_connection("alpha", []) as reader:
            self.assertIsNone(reader)
        with self.assertRaises(team_storage.StorageInputError):
            storage.metadata("alpha", [missing_id] * 9)
        with self.assertRaises(team_storage.StorageInputError):
            storage.metadata("alpha", [missing_id, missing_id])
        self.assertFalse(storage.delete("alpha", missing_id)["deleted"])

        stored = storage.put("alpha", "safe.bin", b"safe")
        with (
            mock.patch.object(storage, "_usage", side_effect=RuntimeError("usage failed")),
            self.assertRaisesRegex(RuntimeError, "usage failed"),
        ):
            storage.delete("alpha", stored["id"])
        self.assertEqual(storage.get("alpha", stored["id"])[1], b"safe")

    def test_destroy_all_counts_only_successful_current_directories(self) -> None:
        storage = team_storage.TeamStorage(self.root, limit_bytes=64)
        storage.put("alpha", "safe.bin", b"safe")
        with mock.patch.object(storage, "destroy", return_value=False) as destroy:
            self.assertEqual(storage.destroy_all(), 0)
        destroy.assert_called_once_with("alpha")


class TeamStorageRetentionTests(unittest.TestCase):
    """Unreferenced files are collected after their grace; references, identical uploads, and Teams are respected."""

    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        self.root = Path(self.temporary_directory.name) / "teams"
        self.now = 1_000_000.0
        self.storage = team_storage.TeamStorage(self.root, limit_bytes=10, clock=lambda: self.now)

    def _ids(self, team_id: str = "alpha") -> list[str]:
        return [item["id"] for item in self.storage.list(team_id)["files"]]

    def test_an_unreferenced_upload_is_collected_exactly_when_its_grace_ends(self) -> None:
        stored = self.storage.put("alpha", "draft.txt", b"draft", "text/plain")
        self.now += team_storage.UNREFERENCED_GRACE_SECONDS - 1
        self.assertEqual(self._ids(), [stored["id"]])
        self.now += 1
        self.assertEqual(self._ids(), [])
        with self.assertRaises(team_storage.StorageNotFoundError):
            self.storage.get("alpha", stored["id"])

    def test_the_sweep_is_idempotent_and_scoped_to_its_team(self) -> None:
        alpha = self.storage.put("alpha", "a.txt", b"alpha", "text/plain")
        beta = self.storage.put("beta", "b.txt", b"beta", "text/plain")
        self.storage.reference("beta", [beta["id"]])
        self.now += team_storage.UNREFERENCED_GRACE_SECONDS
        self.assertEqual(self.storage.sweep("alpha"), 1)
        self.assertEqual(self.storage.sweep("alpha"), 0)
        self.assertEqual(self.storage.sweep("absent"), 0)
        self.assertEqual(self.storage.sweep("beta"), 0)
        self.assertEqual(self._ids("beta"), [beta["id"]])
        with self.assertRaises(team_storage.StorageNotFoundError):
            self.storage.get("alpha", alpha["id"])
        # A reference names only the Team's own files.
        with self.assertRaises(team_storage.StorageNotFoundError):
            self.storage.reference("alpha", [beta["id"]])
        self.assertEqual(self.storage.referenced("alpha"), frozenset())

    def test_a_referenced_file_stays_until_a_later_turn_releases_it_and_then_its_grace(self) -> None:
        sent = self.storage.put("alpha", "sent.txt", b"sent", "text/plain")
        self.storage.reference("alpha", [sent["id"]])
        self.storage.settle("alpha", [sent["id"]])
        self.now += 10 * team_storage.UNREFERENCED_GRACE_SECONDS
        self.assertEqual(self._ids(), [sent["id"]])
        self.assertEqual(self.storage.referenced("alpha"), frozenset({sent["id"]}))
        # A later turn without the file releases it; its grace starts at that release, not at its upload.
        self.storage.settle("alpha", [])
        self.assertEqual(self.storage.referenced("alpha"), frozenset())
        self.now += team_storage.UNREFERENCED_GRACE_SECONDS - 1
        self.assertEqual(self._ids(), [sent["id"]])
        self.now += 1
        self.storage.settle("alpha", [])
        self.assertEqual(self._ids(), [])

    def test_a_new_turn_adds_to_the_previous_references_until_it_completes(self) -> None:
        first = self.storage.put("alpha", "first.txt", b"1", "text/plain")
        second = self.storage.put("alpha", "second.txt", b"2", "text/plain")
        self.storage.reference("alpha", [first["id"]])
        self.storage.settle("alpha", [first["id"]])
        self.storage.reference("alpha", [second["id"]])
        self.assertEqual(self.storage.referenced("alpha"), frozenset({first["id"], second["id"]}))
        self.storage.settle("alpha", [second["id"]])
        self.assertEqual(self.storage.referenced("alpha"), frozenset({second["id"]}))
        self.storage.reference("alpha", [])
        self.storage.settle("absent", [])
        self.assertEqual(self.storage.referenced("absent"), frozenset())

    def test_reference_reports_what_it_added_and_release_returns_only_that(self) -> None:
        held = self.storage.put("alpha", "held.txt", b"1", "text/plain")["id"]
        fresh = self.storage.put("alpha", "fresh.txt", b"2", "text/plain")["id"]
        self.assertEqual(self.storage.reference("alpha", [held]), (held,))
        self.assertEqual(self.storage.reference("alpha", [held, fresh]), (fresh,))
        self.now += 100
        self.storage.release("alpha", [fresh])
        self.storage.release("alpha", [])
        self.storage.release("absent", [fresh])
        self.assertEqual(self.storage.referenced("alpha"), frozenset({held}))
        # The released file's grace starts at its release.
        self.now += team_storage.UNREFERENCED_GRACE_SECONDS - 1
        self.assertEqual(sorted(self._ids()), sorted([held, fresh]))
        self.now += 1
        self.assertEqual(self._ids(), [held])

    def test_a_collected_file_cannot_be_referenced_by_a_turn(self) -> None:
        stored = self.storage.put("alpha", "late.txt", b"late", "text/plain")
        self.now += team_storage.UNREFERENCED_GRACE_SECONDS
        self.storage.sweep("alpha")
        with self.assertRaises(team_storage.StorageNotFoundError):
            self.storage.reference("alpha", [stored["id"]])

    def test_an_identical_upload_reuses_the_stored_file_without_charging_quota(self) -> None:
        stored = self.storage.put("alpha", "same.txt", b"123456", "text/plain")
        self.now += team_storage.UNREFERENCED_GRACE_SECONDS - 1
        again = self.storage.put("alpha", "same.txt", b"123456", "text/plain")
        self.assertEqual((again["id"], again["created_at"]), (stored["id"], stored["created_at"]))
        self.assertEqual((again["used_bytes"], again["remaining_bytes"]), (6, 4))
        # The identical upload restarts the grace period.
        self.now += team_storage.UNREFERENCED_GRACE_SECONDS - 1
        self.assertEqual(self._ids(), [stored["id"]])
        # A different name or type is a different file, charged on its own.
        with self.assertRaises(team_storage.StorageQuotaError):
            self.storage.put("alpha", "other.txt", b"123456", "text/plain")
        self.assertEqual(self.storage.put("beta", "same.txt", b"123456", "text/plain")["used_bytes"], 6)

    def test_reuse_keeps_a_referenced_file_referenced_and_a_full_quota_still_admits_it(self) -> None:
        stored = self.storage.put("alpha", "full.bin", b"1234567890")
        self.storage.reference("alpha", [stored["id"]])
        self.assertEqual(self.storage.put("alpha", "full.bin", b"1234567890")["id"], stored["id"])
        self.assertEqual(self.storage.referenced("alpha"), frozenset({stored["id"]}))

    def test_quota_freed_by_collection_admits_the_next_upload(self) -> None:
        self.storage.put("alpha", "old.bin", b"1234567890")
        with self.assertRaises(team_storage.StorageQuotaError):
            self.storage.put("alpha", "new.bin", b"x")
        self.now += team_storage.UNREFERENCED_GRACE_SECONDS
        fresh = self.storage.put("alpha", "new.bin", b"x")
        self.assertEqual((fresh["used_bytes"], self._ids()), (1, [fresh["id"]]))

    def test_a_database_of_another_schema_is_refused_and_never_upgraded(self) -> None:
        directory = self.root / "alpha"
        directory.mkdir(mode=0o700)
        path = directory / "files.sqlite3"
        with closing(sqlite3.connect(path)) as connection:
            connection.execute("CREATE TABLE files (id TEXT PRIMARY KEY)")
            connection.commit()
        path.chmod(0o600)
        with self.assertRaisesRegex(team_storage.StorageError, "schema is not current"):
            self.storage.list("alpha")
        with closing(sqlite3.connect(path)) as connection:
            self.assertEqual(int(connection.execute("PRAGMA user_version").fetchone()[0]), 0)
            connection.execute("PRAGMA user_version=99")
            connection.commit()
        with self.assertRaisesRegex(team_storage.StorageError, "schema is not current"):
            self.storage.put("alpha", "x.bin", b"x")

    def test_a_schema_another_connection_created_meanwhile_is_accepted(self) -> None:
        self.storage.put("alpha", "x.bin", b"x")
        path = self.root / "alpha" / "files.sqlite3"
        with closing(sqlite3.connect(path, isolation_level=None)) as connection:
            versions = iter([(0,), (team_storage.SCHEMA_VERSION,)])
            raced = mock.Mock(wraps=connection)

            def execute(statement: str, *args: object) -> object:
                if statement == "PRAGMA user_version":
                    return mock.Mock(fetchone=mock.Mock(return_value=next(versions)))
                return connection.execute(statement, *args)

            raced.execute.side_effect = execute
            team_storage.TeamStorage._schema(raced)
            self.assertFalse(connection.in_transaction)

    def test_reference_bookkeeping_failures_map_sqlite_errors(self) -> None:
        self.storage.put("alpha", "x.bin", b"x")
        connection = mock.Mock()
        connection.execute.side_effect = sqlite3.DatabaseError("malformed database")
        for operation in (
            lambda: self.storage.settle("alpha", []),
            lambda: self.storage.reference("alpha", ["0" * 32]),
            lambda: self.storage.sweep("alpha"),
            lambda: self.storage.release("alpha", ["0" * 32]),
            lambda: self.storage.delete("alpha", "0" * 32),
        ):
            with (
                self.subTest(operation=operation),
                mock.patch.object(self.storage, "_connect", return_value=connection),
                self.assertRaisesRegex(team_storage.StorageError, "transaction failed"),
            ):
                operation()


if __name__ == "__main__":
    unittest.main()
