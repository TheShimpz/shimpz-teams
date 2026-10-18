"""Which Team files came from the web and their acknowledgements: marked first, bounded, pruned, fail-closed."""

import hashlib
import stat
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from storage import files as team_storage
from storage import web


def _sha(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


class WebFilesTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.now = 1_800_000_000.0
        self.storage = team_storage.TeamStorage(Path(temporary.name) / "teams", clock=lambda: self.now)
        self.web = web.WebFiles(self.storage, clock=lambda: self.now)

    def test_a_digest_is_marked_before_its_file_exists_and_any_file_with_it_is_from_the_web(self):
        content = b"%PDF-1.4\n"
        self.web.mark("team_1", _sha(content))
        self.assertTrue(self.web.is_web("team_1", _sha(content)))
        stored = self.storage.put("team_1", "edital.pdf", content, "application/pdf")
        self.assertTrue(self.web.is_web("team_1", stored["sha256"]))
        self.assertFalse(self.web.is_web("team_1", _sha(b"another")))
        path = self.storage.team_directory("team_1") / "web.sqlite3"
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)

    def test_an_acknowledgement_binds_the_file_digest_and_principal(self):
        content = b"a,b\n"
        self.web.mark("team_1", _sha(content))
        stored = self.storage.put("team_1", "dados.csv", content, "text/csv")
        self.assertFalse(self.web.acknowledged("team_1", stored["id"], stored["sha256"], "supervisor"))
        self.web.acknowledge("team_1", stored["id"], stored["sha256"], "supervisor")
        self.web.acknowledge("team_1", stored["id"], stored["sha256"], "supervisor")
        self.assertTrue(self.web.acknowledged("team_1", stored["id"], stored["sha256"], "supervisor"))
        # A later prune, past every grace period, keeps the marker and acknowledgement of a file still there.
        self.now += web.ORPHAN_SECONDS
        self.web.mark("team_1", _sha(b"later"))
        self.assertTrue(self.web.is_web("team_1", stored["sha256"]))
        self.assertTrue(self.web.acknowledged("team_1", stored["id"], stored["sha256"], "supervisor"))
        self.assertFalse(self.web.acknowledged("team_1", stored["id"], stored["sha256"], "someone-else"))
        self.assertFalse(self.web.acknowledged("team_2", stored["id"], stored["sha256"], "supervisor"))
        upload = self.storage.put("team_1", "nota.txt", b"plain", "text/plain")
        with self.assertRaisesRegex(team_storage.StorageError, "not from the web"):
            self.web.acknowledge("team_1", upload["id"], upload["sha256"], "supervisor")

    def test_orphan_markers_are_pruned_after_their_grace_period_and_the_store_is_bounded(self):
        self.web.mark("team_1", _sha(b"x"))
        stored = self.storage.put("team_1", "x.bin", b"x", "application/octet-stream")
        self.web.mark("team_1", _sha(b"never stored"))
        gone = mock.patch.object(self.storage, "list", return_value={"files": []})
        with gone:
            # A marker younger than its grace period may still precede its file, so it survives a prune.
            self.web.mark("team_1", _sha(b"fresh"))
            self.assertTrue(self.web.is_web("team_1", _sha(b"never stored")))
            self.now += web.ORPHAN_SECONDS
            self.web.mark("team_1", _sha(b"fresh"))
        self.assertFalse(self.web.is_web("team_1", _sha(b"never stored")))
        self.assertFalse(self.web.is_web("team_1", stored["sha256"]))
        self.assertTrue(self.web.is_web("team_1", _sha(b"fresh")))
        with mock.patch.object(web, "MAX_MARKERS", 1):
            self.web.mark("team_1", _sha(b"fresh"))
            with self.assertRaisesRegex(team_storage.StorageError, "full"):
                self.web.mark("team_1", _sha(b"second"))

    def test_team_teardown_removes_the_store_with_the_team_directory(self):
        self.web.mark("team_1", _sha(b"x"))
        self.storage.destroy("team_1")
        self.assertFalse(self.web.is_web("team_1", _sha(b"x")))

    def test_an_acknowledgement_of_a_file_that_is_gone_is_pruned(self):
        for content in (b"one", b"two"):
            self.web.mark("team_1", _sha(content))
        first = self.storage.put("team_1", "one.bin", b"one", "application/octet-stream")
        self.web.acknowledge("team_1", first["id"], first["sha256"], "supervisor")
        with mock.patch.object(self.storage, "list", return_value={"files": []}):
            second = {"id": "f" * 32, "sha256": _sha(b"two")}
            self.web.acknowledge("team_1", second["id"], second["sha256"], "supervisor")
        self.assertFalse(self.web.acknowledged("team_1", first["id"], first["sha256"], "supervisor"))

    def test_the_acknowledgement_bound_is_enforced(self):
        content = b"y"
        self.web.mark("team_1", _sha(content))
        stored = self.storage.put("team_1", "y.bin", content, "application/octet-stream")
        with mock.patch.object(web, "MAX_ACKNOWLEDGEMENTS", 1):
            self.web.acknowledge("team_1", stored["id"], stored["sha256"], "supervisor")
            with self.assertRaisesRegex(team_storage.StorageError, "full"):
                self.web.acknowledge("team_1", stored["id"], stored["sha256"], "other")

    def test_invalid_inputs_an_unsafe_store_and_an_unreadable_store_all_refuse(self):
        for digest in ("", "A" * 64, "a" * 63, None):
            with self.subTest(digest=digest), self.assertRaisesRegex(team_storage.StorageError, "digest"):
                self.web.is_web("team_1", digest)
        for principal in ("", "a b", "x" * 129, None):
            with self.subTest(principal=principal), self.assertRaisesRegex(team_storage.StorageError, "principal"):
                self.web.acknowledged("team_1", "file", "a" * 64, principal)
        path = self.storage.team_directory("team_1") / "web.sqlite3"
        self.web.mark("team_1", "a" * 64)
        path.chmod(0o644)
        with self.assertRaisesRegex(team_storage.StorageError, "unsafe shape"):
            self.web.is_web("team_1", "a" * 64)
        path.chmod(0o600)
        path.write_bytes(b"not a database" * 100)
        with self.assertRaisesRegex(team_storage.StorageError, "unavailable"):
            self.web.is_web("team_1", "a" * 64)

    def test_a_failure_inside_a_transaction_rolls_it_back(self):
        with (
            mock.patch.object(self.storage, "list", side_effect=team_storage.StorageError("closed")),
            self.assertRaises(team_storage.StorageError),
        ):
            self.web.mark("team_1", "b" * 64)
        self.assertFalse(self.web.is_web("team_1", "b" * 64))


if __name__ == "__main__":
    unittest.main()
