"""A Team's Sign-ins: the capsule Team checks as it arrives, and the sealed store that keeps it."""

import io
import json
import sqlite3
import tarfile
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from local.browser import capsule as capsule_check
from local.browser import sign_ins

TEAM = "neo"
INCARNATION = "n" * 64


def _cookie_store(
    *hosts: str, partitioned: tuple[str, ...] = (), partitioned_under_provider: tuple[str, ...] = ()
) -> bytes:
    """A cookie store with Chromium's table: each host's first-party cookie, and partitioned ones set under a site."""
    connection = sqlite3.connect(":memory:")
    connection.execute("CREATE TABLE cookies (host_key TEXT, top_frame_site_key TEXT, name TEXT, value TEXT)")
    rows = [(host, "") for host in hosts] + [(host, "https://site.example") for host in partitioned]
    rows += [(host, "https://google.com") for host in partitioned_under_provider]
    connection.executemany("INSERT INTO cookies VALUES (?, ?, 'sid', '')", rows)
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

    def test_a_cookie_an_identity_providers_sign_in_host_receives_is_found(self):
        def found(store: bytes) -> frozenset[str]:
            return _checked(_archive([("Default", None), ("Default/Cookies", store)])).identity_hosts()

        self.assertEqual(found(_cookie_store(".accounts.google.com", "x.com")), {"accounts.google.com"})
        # A parent domain's cookie is sent to the sign-in host too; a sibling's or a partitioned one is not.
        self.assertEqual(found(_cookie_store(".google.com")), {"accounts.google.com"})
        self.assertEqual(
            found(_cookie_store(".live.com", ".apple.com")), {"login.live.com", "appleid.apple.com", "idmsa.apple.com"}
        )
        self.assertEqual(
            found(_cookie_store("google.com", "mail.google.com", partitioned=(".google.com",))), frozenset()
        )
        # A cookie partitioned under the provider's own site reaches its sign-in pages.
        self.assertEqual(found(_cookie_store(partitioned_under_provider=(".google.com",))), {"accounts.google.com"})

    def test_a_store_that_is_not_chromiums_table_names_every_identity_provider(self):
        def view_store() -> bytes:
            connection = sqlite3.connect(":memory:")
            connection.execute("CREATE TABLE real (host_key TEXT, top_frame_site_key TEXT)")
            connection.execute("CREATE VIEW cookies AS SELECT * FROM real")
            data = connection.serialize()
            connection.close()
            return data

        def legacy_store() -> bytes:
            connection = sqlite3.connect(":memory:")
            connection.execute("CREATE TABLE cookies (host_key TEXT)")
            data = connection.serialize()
            connection.close()
            return data

        for store in (b"not a database", view_store(), legacy_store()):
            with self.subTest(store=store[:16]):
                check = _checked(_archive([("Default", None), ("Default/Cookies", store)]))
                self.assertEqual(check.identity_hosts(), capsule_check.IDENTITY_HOSTS)
        # A query that would run past its bound of work names every provider too.
        with (
            mock.patch.object(capsule_check, "MAX_SQL_STEPS", 0),
            mock.patch.object(capsule_check, "_PROGRESS_EVERY", 1),
        ):
            check = _checked(_archive([("Default", None), ("Default/Cookies", _cookie_store("x.com"))]))
            self.assertEqual(check.identity_hosts(), capsule_check.IDENTITY_HOSTS)
        # So does a profile without its cookie store, and another store is never let through unchecked.
        self.assertEqual(_checked(_archive([("Default", None)])).identity_hosts(), capsule_check.IDENTITY_HOSTS)
        for other in ("Default/Network/Cookies", "Profile 1/Cookies"):
            parent = other.rpartition("/")[0]
            entries = [("Default", None), ("Default/Cookies", _cookie_store())]
            entries += [(parent, None)] if parent != "Default" else []
            entries += [("Default/Network", None)] if other.startswith("Default/Network") else []
            entries.append((other, _cookie_store()))
            with self.subTest(other=other), self.assertRaises(capsule_check.CapsuleError):
                _checked(_archive(entries))

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

    def test_every_malformed_header_or_bound_is_refused(self):
        valid = _archive([("Default", None), ("Default/Cookies", b"cookie")])

        def with_header(change):
            data = bytearray(valid)
            change(data)
            header = data[:512]
            header[148:156] = b" " * 8
            data[148:156] = f"{sum(header):06o}\0 ".encode()
            return bytes(data)

        def device(data):
            data[156:157] = b"3"

        def not_utf8(data):
            data[0:2] = b"\xff\xfe"

        def not_octal(data):
            data[124:136] = b"zzzzzzzzzzz\0"

        def not_ustar(data):
            data[257:263] = b"gnutar"

        padding = bytearray(valid)
        padding[512 + 512 + 6] = 1
        end_then_entry = valid[: len(valid) - 1024] + bytes(512) + valid[512:1024] + bytes(1024)
        for data in (
            with_header(device),
            with_header(not_utf8),
            with_header(not_octal),
            with_header(not_ustar),
            bytes(padding),
            end_then_entry,
        ):
            with self.subTest(data=data[:8]), self.assertRaises(capsule_check.CapsuleError):
                _checked(data, len(data))
        check = _checked(valid)
        with self.assertRaises(capsule_check.CapsuleError):
            check.feed(b"\0")
        for bound, value in (("MAX_ENTRIES", 1), ("MAX_TOTAL_BYTES", 2)):
            with (
                self.subTest(bound=bound),
                mock.patch.object(capsule_check, bound, value),
                self.assertRaises(capsule_check.CapsuleError),
            ):
                _checked(valid)


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

    def test_a_team_deleted_during_a_save_takes_its_chunks_and_a_failed_removal_keeps_the_record(self):
        draft = self.store.draft(TEAM, INCARNATION)
        draft.add(b"first")
        unwritten = self.store.draft(TEAM, INCARNATION)
        self.assertTrue(any((self.root / "state" / "drafts" / TEAM).iterdir()))
        self.assertTrue(self.store.delete_team(TEAM))
        self.assertFalse((self.root / "state" / "drafts" / TEAM).exists())
        with self.assertRaises(sign_ins.SignInError):
            draft.commit("www.linkedin.com", "Work", "image", lambda: True)
        # A draft begun before the deletion never writes its first chunk after it, nor brings the Team's drafts back.
        with self.assertRaises(sign_ins.SignInError):
            unwritten.add(b"late")
        self.assertFalse((self.root / "state" / "drafts" / TEAM).exists())
        saved = self.save()
        with (
            mock.patch.object(sign_ins.shutil, "rmtree", side_effect=PermissionError("held")),
            self.assertRaises(sign_ins.SignInError),
        ):
            self.store.forget(TEAM, saved["sign_in_id"])
        # Nothing was proven gone, so the Sign-in is still listed and can be forgotten again.
        self.assertEqual(self.store.list(TEAM), [saved])
        self.assertTrue(self.store.forget(TEAM, saved["sign_in_id"]))

    def test_a_starting_team_deletes_what_an_interrupted_save_left_and_keeps_every_sign_in(self):
        saved = self.save()
        draft = self.store.draft(TEAM, INCARNATION)
        draft.add(b"interrupted")
        orphan = self.root / "state" / "capsules" / f"{'d' * 32}-1"
        orphan.mkdir(mode=0o700)
        (orphan / "00000.chunk").write_bytes(b"left")
        self.store.reconcile()
        self.assertFalse((self.root / "state" / "drafts").exists())
        self.assertFalse(orphan.exists())
        self.assertEqual(list(self.store.capsule(TEAM, saved["sign_in_id"])), [b"first", b"second"])
        self.store.delete_all()
        self.store.reconcile()
        with (
            mock.patch.object(sign_ins.Path, "unlink", side_effect=PermissionError("held")),
            self.assertRaises(sign_ins.SignInError),
        ):
            self.store.delete_all()

    def test_a_chunk_that_cannot_be_written_is_not_kept(self):
        kept = self.save()
        with (
            mock.patch.object(sign_ins.private_state, "replace_durably", side_effect=OSError("full")),
            self.assertRaises(sign_ins.SignInError),
        ):
            self.save()
        self.assertEqual(self.store.list(TEAM), [kept])

    def test_a_capsule_that_cannot_be_put_in_place_is_not_kept(self):
        with (
            mock.patch.object(sign_ins.Path, "rename", side_effect=OSError("cross-device")),
            self.assertRaises(sign_ins.SignInError),
        ):
            self.save()
        self.assertEqual(self.store.list(TEAM), [])

    def test_a_reset_deletes_every_sign_in_and_every_capsule(self):
        self.save()
        self.save(team="trinity")
        self.store.draft(TEAM, INCARNATION).add(b"under way")
        self.store.delete_all()
        self.assertEqual((self.store.list(TEAM), self.store.list("trinity")), ([], []))
        self.assertFalse((self.root / "state" / "capsules").exists())
        self.assertFalse((self.root / "state" / "drafts").exists())

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

    def test_a_state_or_record_not_this_stores_own_is_refused(self):
        index = self.root / "state" / "index.json"
        for written in (b"not json", b"[]", b'{"schema": 1, "teams": {"neo": []}}'):
            with self.subTest(written=written):
                index.write_bytes(written)
                index.chmod(0o600)
                with self.assertRaises(sign_ins.SignInError):
                    self.store.list(TEAM)
        index.unlink()
        sign_in_id = "a" * 32
        key = self.store._key(create=True)
        aad = sign_ins._record_aad(TEAM, sign_in_id)
        for plaintext in (b"not json", b'{"team_id": "neo"}'):
            with self.subTest(plaintext=plaintext):
                envelope = sign_ins.private_state.seal_identified(key, plaintext, aad)
                index.write_text(json.dumps({"schema": 1, "teams": {TEAM: {sign_in_id: envelope}}}))
                index.chmod(0o600)
                with self.assertRaises(sign_ins.SignInError):
                    self.store.list(TEAM)
        index.unlink()

    def test_its_bounds_and_absences_are_answered_exactly(self):
        with mock.patch.object(sign_ins, "MAX_SIGN_INS", 1):
            self.save()
            with self.assertRaises(sign_ins.SignInError):
                self.store.draft(TEAM, INCARNATION)
        with self.assertRaises(sign_ins.SignInError):
            list(self.store.capsule(TEAM, "c" * 32))
        with self.assertRaises(sign_ins.SignInError):
            self.store.forget(TEAM, "../escape")

    def test_a_deleted_team_takes_every_sign_in_and_a_reset_that_leaves_any_fails(self):
        self.save()
        self.save()
        self.assertTrue(self.store.delete_team(TEAM))
        self.assertEqual(list((self.root / "state" / "capsules").iterdir()), [])
        self.save()
        with mock.patch.object(sign_ins.shutil, "rmtree"), self.assertRaises(sign_ins.SignInError):
            self.store.delete_all()


if __name__ == "__main__":
    unittest.main()
