"""A finished task's downloads: strictly listed, read exactly, typed from bytes, stored as from the web, bounded."""

import unittest

from browser import ingest
from storage import files as team_storage

PDF = b"%PDF-1.4\n"


def _listing(files=(), refused=(), skipped=0):
    return {
        "files": [{"name": name, "size": size} for name, size in files],
        "refused": list(refused),
        "skipped": skipped,
    }


class Recorder:
    def __init__(self, contents=None, errors=None):
        self.contents = contents or {}
        self.errors = errors or {}
        self.events = []

    def read(self, name, size):
        self.events.append(("read", name, size))
        if self.errors.get(name) == "read":
            raise ingest.ReadLostError(name)
        return self.contents[name]

    def put(self, name, data, media_type):
        self.events.append(("put", name, media_type))
        error = self.errors.get(name)
        if isinstance(error, BaseException):
            raise error
        return {"id": f"id-{name}", "name": name, "media_type": media_type, "size": len(data), "sha256": "x"}


def _running():
    return False


class IngestTests(unittest.TestCase):
    def test_each_kept_file_is_typed_from_its_bytes_before_it_is_stored(self):
        recorder = Recorder({"edital.pdf": PDF, "dados.csv": b"a,b\n1,2\n"})
        result = ingest.ingest(_listing([("edital.pdf", 9), ("dados.csv", 8)]), recorder.read, recorder.put, _running)
        self.assertEqual(
            result.kept,
            (
                {"id": "id-edital.pdf", "name": "edital.pdf", "media_type": "application/pdf", "size": 9},
                {"id": "id-dados.csv", "name": "dados.csv", "media_type": "text/csv", "size": 8},
            ),
        )
        self.assertEqual((result.lost, result.skipped), ((), 0))
        self.assertEqual(
            recorder.events[:2],
            [("read", "edital.pdf", 9), ("put", "edital.pdf", "application/pdf")],
        )

    def test_every_file_not_kept_has_its_closed_reason(self):
        files = [(f"f{index}.bin", 1) for index in range(ingest.MAX_FILES)]
        recorder = Recorder(
            {name: b"\x00" for name, _ in files},
            {
                "f1.bin": team_storage.StorageQuotaError("full"),
                "f2.bin": team_storage.StorageInputError("name"),
                "f3.bin": team_storage.StorageError("closed"),
            },
        )
        listing = _listing(
            [*files, ("extra.bin", 1), ("huge.iso", ingest.MAX_FILE_BYTES + 1), ("empty.txt", 0)],
            refused=[{"name": "Unconfirmed 1.crdownload", "reason": "incomplete", "size": 7}],
            skipped=2,
        )
        result = ingest.ingest(listing, recorder.read, recorder.put, _running)
        self.assertEqual(len(result.kept), ingest.MAX_FILES - 3)
        self.assertEqual(
            result.lost,
            (
                {"name": "Unconfirmed 1.crdownload", "reason": "incomplete"},
                {"name": "f1.bin", "reason": "quota-exceeded"},
                {"name": "f2.bin", "reason": "name-refused"},
                {"name": "f3.bin", "reason": "store-unavailable"},
                {"name": "extra.bin", "reason": "over-limit"},
                {"name": "huge.iso", "reason": "over-limit"},
                {"name": "empty.txt", "reason": "over-limit"},
            ),
        )
        self.assertEqual(result.skipped, 2)

    def test_an_empty_download_is_not_kept(self):
        recorder = Recorder()
        result = ingest.ingest(_listing([("empty.txt", 0)]), recorder.read, recorder.put, _running)
        self.assertEqual(
            (result.kept, result.lost, recorder.events), ((), ({"name": "empty.txt", "reason": "empty"},), [])
        )

    def test_a_lost_read_ends_the_ingest_and_names_every_later_file(self):
        recorder = Recorder({"a.pdf": PDF}, {"b.pdf": "read"})
        result = ingest.ingest(
            _listing([("a.pdf", 9), ("b.pdf", 9), ("c.pdf", 9)]), recorder.read, recorder.put, _running
        )
        self.assertEqual([item["name"] for item in result.kept], ["a.pdf"])
        self.assertEqual(
            result.lost, ({"name": "b.pdf", "reason": "browser-lost"}, {"name": "c.pdf", "reason": "browser-lost"})
        )

    def test_a_stop_ends_the_ingest_before_the_next_file(self):
        recorder = Recorder({"a.pdf": PDF, "b.pdf": PDF})
        # Each file is checked before it is read and again before it is stored.
        stops = iter((False, False, True))
        result = ingest.ingest(_listing([("a.pdf", 9), ("b.pdf", 9)]), recorder.read, recorder.put, lambda: next(stops))
        self.assertEqual([item["name"] for item in result.kept], ["a.pdf"])
        self.assertNotIn(("read", "b.pdf", 9), recorder.events)

    def test_a_stop_during_a_read_stores_nothing(self):
        stopped = [False]

        class Stopping(Recorder):
            def read(self, name, size):
                stopped[0] = True
                return super().read(name, size)

        recorder = Stopping({"a.pdf": PDF})
        result = ingest.ingest(_listing([("a.pdf", 9)]), recorder.read, recorder.put, lambda: stopped[0])
        self.assertEqual((result.kept, [event[0] for event in recorder.events]), ((), ["read"]))

    def test_a_store_refused_for_stop_ends_the_ingest(self):
        recorder = Recorder({"a.pdf": PDF, "b.pdf": PDF}, {"a.pdf": ingest.StoppedError("a.pdf")})
        result = ingest.ingest(_listing([("a.pdf", 9), ("b.pdf", 9)]), recorder.read, recorder.put, _running)
        self.assertEqual((result.kept, result.lost), ((), ()))
        self.assertNotIn(("read", "b.pdf", 9), recorder.events)

    def test_a_malformed_listing_is_refused_whole(self):
        for listing in (
            {},
            {"files": [], "refused": [], "skipped": -1},
            {"files": [], "refused": [], "skipped": True},
            {"files": "x", "refused": [], "skipped": 0},
            {"files": [{"name": "a"}], "refused": [], "skipped": 0},
            {"files": [{"name": "", "size": 1}], "refused": [], "skipped": 0},
            {"files": [{"name": "a", "size": -1}], "refused": [], "skipped": 0},
            {"files": [{"name": "a", "size": 1.0}], "refused": [], "skipped": 0},
            {"files": ["a"], "refused": [], "skipped": 0},
            {"files": [], "refused": [{"name": "a", "reason": "other", "size": 1}], "skipped": 0},
            {"files": [], "refused": [{"name": "", "reason": "incomplete", "size": 1}], "skipped": 0},
            {"files": [], "refused": ["a"], "skipped": 0},
            {"files": [{"name": "a", "size": 1}] * (ingest.MAX_LISTED + 1), "refused": [], "skipped": 0},
        ):
            with self.subTest(listing=str(listing)[:80]), self.assertRaises(ingest.ListingError):
                ingest.ingest(listing, None, None, None)


class PresentTests(unittest.TestCase):
    def listing(self, files=(), refused=(), skipped=0):
        return {
            "files": [{"name": name, "size": 1} for name in files],
            "refused": [{"name": name, "reason": reason, "size": 1} for name, reason in refused],
            "skipped": skipped,
        }

    def test_a_step_sees_completed_and_over_limit_files_and_counts_the_ones_still_downloading(self):
        listing = self.listing(["b.csv", "a.csv"], [("c.crdownload", "incomplete"), ("big.zip", "over-limit")])
        self.assertEqual(
            ingest.present(listing), {"files": ["a.csv", "b.csv", "big.zip"], "pending": 1, "truncated": False}
        )

    def test_names_left_out_mark_the_list_truncated(self):
        many = [f"f{index:02d}.csv" for index in range(25)]
        shown = ingest.present(self.listing(many))
        self.assertEqual((len(shown["files"]), shown["truncated"]), (20, True))
        self.assertTrue(ingest.present(self.listing(["ok.csv", "bad\nname.csv"]))["truncated"])
        self.assertEqual(ingest.present(self.listing(["ok.csv", "bad\nname.csv"]))["files"], ["ok.csv"])
        self.assertTrue(ingest.present(self.listing(skipped=2))["truncated"])
        with self.assertRaises(ingest.ListingError):
            ingest.present({"files": "x", "refused": [], "skipped": 0})


if __name__ == "__main__":
    unittest.main()
