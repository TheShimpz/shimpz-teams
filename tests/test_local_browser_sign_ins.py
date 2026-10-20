"""A Team's Sign-ins: the capsule Team checks as it arrives, and the sealed store that keeps it."""

import io
import json
import sqlite3
import tarfile
import tempfile
import unittest
from pathlib import Path

from local.browser import capsule as capsule_check
from local.browser import sign_ins

TEAM = "neo"
INCARNATION = "n" * 64


def _cookie_store(*hosts: str) -> bytes:
    connection = sqlite3.connect(":memory:")
    connection.execute("CREATE TABLE cookies (host_key TEXT, name TEXT, value TEXT)")
    connection.executemany("INSERT INTO cookies VALUES (?, 'sid', '')", [(host,) for host in hosts])
    connection.commit()
    data = connection.serialize()
    connection.close()
    return data


def _archive(entries: list[tuple[str, bytes | None]]) -> bytes:
    """A ustar archive of directories (None) and files, as `browser-host profile export` writes one."""
    out = io.BytesIO()
    with tarfile.open(fileobj=out, mode="w", format=tarfile.USTAR_FORMAT) as archive:
        for name, body in entries:
            info = tarfile.TarInfo(name)
            if body is None:
                info.type, info.mode = tarfile.DIRTYPE, 0o700
                archive.addfile(info)
            else:
                info.size, info.mode = len(body), 0o600
                archive.addfile(info, io.BytesIO(body))
        end = archive.offset
    # Only the two end blocks, as `browser-host` writes them; tarfile pads to a 10 KiB record.
    return out.getvalue()[: end + 1024]


def _checked(data: bytes, step: int = 700) -> capsule_check.Check:
    check = capsule_check.Check()
    for start in range(0, len(data), step):
        check.feed(data[start : start + step])
    check.finish()
    return check


class CapsuleCheckTests(unittest.TestCase):
    def test_a_profile_capsule_passes_whatever_its_chunking_and_keeps_only_the_cookie_store(self):
        cookies = _cookie_store(".linkedin.com", "www.linkedin.com")
        data = _archive([("Default", None), ("Default/Cookies", cookies), ("Default/Preferences", b"{}")])
        for step in (1, 511, 512, 4096, len(data)):
            check = _checked(data, step)
            self.assertEqual(bytes(check.cookies), cookies)
            self.assertEqual(check.identity_hosts(), frozenset())

    def test_a_session_at_an_identity_providers_sign_in_host_is_found(self):
        data = _archive([("Default", None), ("Default/Cookies", _cookie_store(".accounts.google.com", "x.com"))])
        self.assertEqual(_checked(data).identity_hosts(), {"accounts.google.com"})
        unreadable = _archive([("Default", None), ("Default/Cookies", b"not a database")])
        self.assertEqual(_checked(unreadable).identity_hosts(), capsule_check.IDENTITY_HOSTS)

    def test_every_hostile_or_malformed_capsule_is_refused(self):
        valid = _archive([("Default", None), ("Default/Cookies", b"c")])
        link = io.BytesIO()
        with tarfile.open(fileobj=link, mode="w", format=tarfile.USTAR_FORMAT) as archive:
            info = tarfile.TarInfo("Default")
            info.type = tarfile.SYMTYPE
            info.linkname = "/etc"
            archive.addfile(info)
        bad_sum = bytearray(valid)
        bad_sum[0] ^= 1
        for data in (
            link.getvalue(),
            _archive([("Default/Cookies", b"orphan")]),
            _archive([("../escape", b"x")]),
            _archive([("Default", None), ("Default", None)]),
            bytes(bad_sum),
            valid[:-512],
            valid + b"\x01",
        ):
            with self.subTest(data=data[:20]), self.assertRaises(capsule_check.CapsuleError):
                _checked(data)


class SignInStoreTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        (self.root / "state").mkdir(mode=0o700)
        (self.root / "key").mkdir(mode=0o700)
        self.now = 1_800_000_000.0
        self.store = sign_ins.SignInStore(self.root / "state", self.root / "key" / "aes256.key", now=lambda: self.now)

    def save(self, chunks=(b"first", b"second"), admitted=lambda: True, team=TEAM, site="www.linkedin.com"):
        draft = self.store.draft(team, INCARNATION)
        try:
            for chunk in chunks:
                draft.add(chunk)
            return draft.commit(site, " Work ", "image@sha256:x", admitted)
        except BaseException:
            draft.discard()
            raise

    def test_a_saved_sign_in_lists_without_its_capsule_and_restores_exactly(self):
        saved = self.save()
        self.assertEqual(set(saved), {"sign_in_id", "site", "label", "created_at", "last_used_at", "expires_at"})
        self.assertEqual((saved["site"], saved["label"]), ("www.linkedin.com", "Work"))
        self.assertEqual(self.store.list(TEAM), [saved])
        self.assertEqual(list(self.store.capsule(TEAM, saved["sign_in_id"])), [b"first", b"second"])
        self.assertEqual(self.store.list("other"), [])
        # Nothing of the capsule or the record is readable without the key.
        stored = b"".join(path.read_bytes() for path in (self.root / "state").rglob("*") if path.is_file())
        self.assertNotIn(b"first", stored)
        self.assertNotIn(b"Work", stored)

    def test_an_altered_record_or_a_moved_chunk_is_refused(self):
        saved = self.save()
        sign_in_id = saved["sign_in_id"]
        chunks = sorted((self.root / "state" / "capsules").rglob("*.chunk"))
        first, second = chunks[0].read_bytes(), chunks[1].read_bytes()
        chunks[0].write_bytes(second)
        chunks[1].write_bytes(first)
        with self.assertRaises(sign_ins.SignInError):
            list(self.store.capsule(TEAM, sign_in_id))
        index = self.root / "state" / "index.json"
        state = json.loads(index.read_bytes())
        state["teams"]["other"] = state["teams"].pop(TEAM)
        index.write_text(json.dumps(state))
        with self.assertRaises(sign_ins.SignInError):
            self.store.list("other")

    def test_a_sign_in_expires_after_its_lifetime_or_a_week_unused(self):
        self.save()
        self.now += sign_ins.IDLE_SECONDS
        self.assertEqual(self.store.list(TEAM), [])
        self.assertEqual(list((self.root / "state" / "capsules").iterdir()), [])

    def test_forget_and_team_deletion_leave_nothing_and_fence_a_save_under_way(self):
        saved = self.save()
        self.assertTrue(self.store.forget(TEAM, saved["sign_in_id"]))
        self.assertFalse(self.store.forget(TEAM, saved["sign_in_id"]))

        def deleted_meanwhile():
            yield b"first"
            self.store.delete_team(TEAM)
            yield b"second"

        with self.assertRaises(sign_ins.SignInError):
            self.save(deleted_meanwhile())
        with self.assertRaises(sign_ins.SignInError):
            self.save(admitted=lambda: False)
        self.assertEqual(self.store.list(TEAM), [])
        self.assertEqual(list((self.root / "state" / "capsules").iterdir()), [])

    def test_a_reset_deletes_every_sign_in_and_every_capsule(self):
        self.save()
        self.save(team="trinity")
        self.store.delete_all()
        self.assertEqual((self.store.list(TEAM), self.store.list("trinity")), ([], []))
        self.assertFalse((self.root / "state" / "capsules").exists())

    def test_only_an_admissible_site_label_and_bounded_capsule_are_kept(self):
        for site, label in (("localhost", "Work"), ("www.linkedin.com", ""), ("www.linkedin.com", "x" * 65)):
            with self.subTest(site=site, label=label), self.assertRaises(sign_ins.SignInError):
                draft = self.store.draft(TEAM, INCARNATION)
                draft.add(b"x")
                draft.commit(site, label, "image", lambda: True)
        for chunks in ((), (b"",), (b"x" * (sign_ins.CHUNK_BYTES + 1),)):
            with self.subTest(chunks=len(chunks)), self.assertRaises(sign_ins.SignInError):
                self.save(chunks)
        self.assertEqual(self.store.list(TEAM), [])


if __name__ == "__main__":
    unittest.main()
