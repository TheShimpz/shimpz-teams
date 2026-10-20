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
WRITER = "image@sha256:x"
EVENT = "e" * 32
DELETING = {"event_id": EVENT, "initiator_id": "s" * 32, "factor": "totp", "origin": "https://admin.example"}


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

    def current(self, team=TEAM):
        return self.store.current(team, WRITER)

    def checkout(self, team=TEAM, kind=sign_ins.PERSON):
        return self.store.checkout(team, INCARNATION, kind)

    def save(self, chunks=(b"first", b"second"), admitted=lambda: True, team=TEAM, sites=("linkedin.com",)):
        """Check the Team's Sign-in out and Save its next generation."""
        checkout = self.checkout(team)
        draft = self.store.draft(checkout)
        try:
            for chunk in chunks:
                draft.add(chunk)
            return draft.commit(list(sites), False, WRITER, admitted)
        except BaseException:
            draft.discard()
            self.store.finish(checkout)
            raise

    def capsules(self):
        directory = self.root / "state" / "capsules"
        return sorted(path.name for path in directory.iterdir()) if directory.is_dir() else []

    def test_a_saved_sign_in_shows_without_its_capsule_and_restores_exactly(self):
        self.assertIsNone(self.current(TEAM))
        saved = self.save(sites=("linkedin.com", "example.com"))
        self.assertEqual(
            set(saved),
            {
                "sign_in_id",
                "state",
                "generation",
                "team_incarnation",
                "sites",
                "sites_truncated",
                "created_at",
                "saved_at",
                "last_used_at",
                "expires_at",
            },
        )
        self.assertEqual(
            (saved["state"], saved["generation"], saved["sites"], saved["team_incarnation"]),
            ("ready", 1, ["example.com", "linkedin.com"], INCARNATION),
        )
        self.assertEqual(self.current(TEAM), saved)
        self.assertIsNone(self.current("other"))
        checkout = self.checkout()
        self.assertEqual(checkout.generation, 1)
        self.assertEqual(list(self.store.capsule(checkout, WRITER)), [b"first", b"second"])
        # Nothing of the capsule or the record is readable without the key.
        stored = b"".join(path.read_bytes() for path in (self.root / "state").rglob("*") if path.is_file())
        self.assertNotIn(b"first", stored)
        self.assertNotIn(b"linkedin", stored)

    def test_a_save_writes_the_next_generation_and_replaces_the_one_it_restored(self):
        first = self.save()
        second = self.save(chunks=(b"third",), sites=("github.com",))
        self.assertEqual(
            (second["sign_in_id"], second["generation"], second["sites"]), (first["sign_in_id"], 2, ["github.com"])
        )
        self.assertEqual(self.capsules(), [f"{first['sign_in_id']}-2"])
        self.assertEqual(list(self.store.capsule(self.checkout(), WRITER)), [b"third"])

    def test_the_sign_in_has_one_checkout_at_a_time_and_a_stale_end_changes_nothing(self):
        self.save()
        checkout = self.checkout()
        with self.assertRaises(sign_ins.SignInError) as busy:
            self.checkout()
        self.assertEqual(busy.exception.code, "sign-in-busy")
        draft = self.store.draft(checkout)
        draft.add(b"x")
        self.store.finish(checkout)
        # An end without a Save leaves the restored generation needing a person; a Save of that checkout is refused.
        self.assertEqual((self.current(TEAM)["state"], self.current(TEAM)["generation"]), ("needs-person", 1))
        with self.assertRaises(sign_ins.SignInError):
            draft.commit(["linkedin.com"], False, "image", lambda: True)
        self.store.finish(checkout)
        with self.assertRaises(sign_ins.SignInError) as refused:
            self.checkout(kind=sign_ins.AGENT)
        self.assertEqual(refused.exception.code, "sign-in-busy")
        # A person reconciles it: their Save makes it ready again.
        self.assertEqual(self.save()["state"], "ready")
        for stale in (lambda: self.store.capsule(checkout, WRITER), lambda: self.store.draft(checkout)):
            with self.subTest(stale=stale), self.assertRaises(sign_ins.SignInError):
                stale()

    def test_a_teams_first_checkout_that_ends_without_a_save_leaves_no_sign_in(self):
        checkout = self.checkout()
        self.assertEqual(checkout.generation, 0)
        self.assertIsNone(self.store.capsule(checkout, WRITER))
        self.assertIsNone(self.current(TEAM))
        self.store.finish(checkout)
        self.assertEqual(self.store._state(), {})

    def test_a_sign_in_expires_after_a_month_from_its_save_or_a_week_unused(self):
        self.save()
        self.now += sign_ins.IDLE_SECONDS
        self.assertIsNone(self.current(TEAM))
        self.assertEqual(self.capsules(), [])
        self.save()
        for _week in range(4):
            self.now += sign_ins.IDLE_SECONDS - 60
            self.store.finish(self.checkout())
        self.now += 4 * 24 * 3600
        self.assertIsNone(self.current(TEAM))

    def test_deletion_needs_the_exact_confirmed_sign_in_and_fences_a_save_under_way(self):
        saved = self.save()
        confirmed = (saved["sign_in_id"], saved["generation"], INCARNATION)
        for expected, code in (
            ((saved["sign_in_id"], 2, INCARNATION), "sign-in-changed"),
            (("b" * 32, 1, INCARNATION), "sign-in-changed"),
            ((saved["sign_in_id"], 1, "m" * 64), "sign-in-changed"),
        ):
            with self.subTest(expected=expected), self.assertRaises(sign_ins.SignInError) as refused:
                self.store.begin_delete(TEAM, expected, DELETING)
            self.assertEqual(refused.exception.code, code)
        for metadata in ({**DELETING, "event_id": ""}, {"event_id": EVENT}, {**DELETING, "factor": 1}):
            with self.subTest(metadata=metadata), self.assertRaises(sign_ins.SignInError):
                self.store.begin_delete(TEAM, confirmed, metadata)
        checkout = self.checkout()
        draft = self.store.draft(checkout)
        draft.add(b"under way")
        self.store.begin_delete(TEAM, confirmed, DELETING)
        with self.assertRaises(sign_ins.SignInError):
            draft.add(b"after the fence")
        self.assertTrue(self.store.purge(TEAM, EVENT))
        self.assertIsNone(self.current())
        self.assertEqual(self.capsules(), [])
        self.assertFalse((self.root / "state" / "drafts" / TEAM).exists())
        # Until its completion is recorded, the fenced record keeps the deletion's metadata and every checkout out;
        # another Team's Sign-in is no deletion.
        self.save(team="trinity")
        named = {"generation": 1, "incarnation": INCARNATION}
        self.assertEqual(self.store.deletions(), [(TEAM, {**DELETING, **named})])
        for refused_call in (self.checkout, lambda: self.store.begin_delete(TEAM, confirmed, DELETING)):
            with self.subTest(refused=refused_call), self.assertRaises(sign_ins.SignInError):
                refused_call()
        self.assertTrue(self.store.settle(TEAM, EVENT))
        self.assertEqual((list(self.store._state()), self.store.deletions()), (["trinity"], []))
        with self.assertRaises(sign_ins.SignInError) as absent:
            self.store.begin_delete(TEAM, confirmed, DELETING)
        self.assertEqual(absent.exception.code, "sign-in-absent")
        # Another deletion's purge or settle, or one already settled, leaves a Sign-in started afterwards as it is.
        saved = self.save()
        self.assertTrue(self.store.purge(TEAM, EVENT))
        self.assertTrue(self.store.settle(TEAM, EVENT))
        self.assertEqual(self.current()["generation"], 1)

    def test_a_deletion_it_cannot_prove_keeps_its_fence_and_completes_later(self):
        saved = self.save()
        self.store.begin_delete(TEAM, (saved["sign_in_id"], 1, INCARNATION), DELETING)
        with mock.patch.object(sign_ins.shutil, "rmtree", side_effect=PermissionError("held")):
            self.assertFalse(self.store.purge(TEAM, EVENT))
        # The fence and its metadata survive a restart, whose start removes what it left.
        restarted = sign_ins.SignInStore(self.root / "state", self.root / "key" / "aes256.key", now=lambda: self.now)
        self.assertIsNone(restarted.current(TEAM, WRITER))
        restarted.reconcile()
        self.assertEqual(self.capsules(), [])
        self.assertEqual([team for team, _metadata in restarted.deletions()], [TEAM])
        self.assertTrue(restarted.settle(TEAM, EVENT))
        self.assertEqual(restarted._state(), {})

    def test_a_starting_team_ends_open_checkouts_and_deletes_what_was_left(self):
        self.save()
        self.checkout()
        orphan = self.root / "state" / "capsules" / f"{'d' * 32}-1"
        orphan.mkdir(mode=0o700)
        draft = self.store.draft(self.store.checkout("trinity", INCARNATION, sign_ins.PERSON))
        draft.add(b"interrupted")
        self.store.reconcile()
        self.assertEqual(self.current(TEAM)["state"], "needs-person")
        self.assertNotIn("trinity", self.store._state())
        self.assertFalse(orphan.exists())
        self.assertFalse((self.root / "state" / "drafts").exists())
        # A lifecycle deletion left undone is completed outright; a Supervisor's keeps its record until it is recorded.
        with mock.patch.object(sign_ins.shutil, "rmtree", side_effect=PermissionError("held")):
            self.assertFalse(self.store.delete_team(TEAM))
        self.store.reconcile()
        self.assertEqual(self.store._state(), {})
        saved = self.save()
        self.store.begin_delete(TEAM, (saved["sign_in_id"], 1, INCARNATION), DELETING)
        self.store.reconcile()
        self.assertEqual((self.capsules(), len(self.store.deletions())), ([], 1))
        self.store.delete_all()
        self.store.reconcile()

    def test_a_sign_in_of_an_earlier_team_incarnation_is_gone_before_a_new_checkout(self):
        self.save()
        checkout = self.store.checkout(TEAM, "m" * 64, sign_ins.PERSON)
        self.assertEqual(checkout.generation, 0)
        self.assertEqual(self.capsules(), [])

    def test_a_team_deletion_and_a_reset_take_everything_and_fence_saves(self):
        self.save()
        draft = self.store.draft(self.checkout())
        draft.add(b"under way")
        self.assertTrue(self.store.delete_team(TEAM))
        with self.assertRaises(sign_ins.SignInError):
            draft.commit(["linkedin.com"], False, "image", lambda: True)
        self.assertTrue(self.store.delete_team("nobody"))
        self.save()
        self.save(team="trinity")
        late = self.store.draft(self.store.checkout("neo2", INCARNATION, sign_ins.PERSON))
        self.store.delete_all()
        self.assertEqual((self.current(TEAM), self.current("trinity")), (None, None))
        self.assertFalse((self.root / "state" / "capsules").exists())
        with self.assertRaises(sign_ins.SignInError):
            late.add(b"after the reset")
        with (
            mock.patch.object(sign_ins.Path, "unlink", side_effect=PermissionError("held")),
            self.assertRaises(sign_ins.SignInError),
        ):
            self.store.delete_all()

    def test_only_an_admissible_bounded_capsule_with_admissible_sites_is_kept(self):
        for sites in ((), ("Example.com",)):
            with self.subTest(sites=sites), self.assertRaises(sign_ins.SignInError):
                self.save(chunks=() if not sites else (b"x",), sites=sites or ("linkedin.com",))
        for chunks in ((b"",), (b"x" * (sign_ins.CHUNK_BYTES + 1),)):
            with self.subTest(chunks=len(chunks)), self.assertRaises(sign_ins.SignInError):
                self.save(chunks)
        with self.assertRaises(sign_ins.SignInError):
            self.save(admitted=lambda: False)
        checkout = self.checkout()
        draft = self.store.draft(checkout)
        with (
            mock.patch.object(sign_ins.private_state, "replace_durably", side_effect=OSError("full")),
            self.assertRaises(sign_ins.SignInError),
        ):
            draft.add(b"x")
        draft.discard()
        self.store.finish(checkout)
        with (
            mock.patch.object(sign_ins.Path, "rename", side_effect=OSError("cross-device")),
            self.assertRaises(sign_ins.SignInError),
        ):
            self.save()
        self.assertIsNone(self.current(TEAM))

    def test_a_removal_that_leaves_a_capsule_in_place_proves_nothing(self):
        saved = self.save()
        self.store.begin_delete(TEAM, (saved["sign_in_id"], 1, INCARNATION), DELETING)
        with mock.patch.object(sign_ins.shutil, "rmtree"):
            self.assertFalse(self.store.purge(TEAM, EVENT))
        self.assertTrue(self.store.purge(TEAM, EVENT))
        self.save(team="trinity")
        with mock.patch.object(sign_ins.shutil, "rmtree"), self.assertRaises(sign_ins.SignInError) as busy:
            # A new incarnation's checkout must delete the earlier Sign-in first, and cannot.
            self.store.checkout("trinity", "m" * 64, sign_ins.PERSON)
        self.assertEqual(busy.exception.code, "sign-in-busy")

    def test_a_team_deletion_or_a_reset_keeps_only_a_pending_deletions_metadata(self):
        saved = self.save()
        self.save(team="trinity")
        self.store.begin_delete(TEAM, (saved["sign_in_id"], 1, INCARNATION), DELETING)
        self.assertTrue(self.store.delete_team(TEAM))
        named = {"generation": 1, "incarnation": INCARNATION}
        self.assertEqual(self.store.deletions(), [(TEAM, {**DELETING, **named})])
        self.store.delete_all()
        self.assertEqual((self.capsules(), list(self.store._state())), ([], [TEAM]))
        self.assertEqual(self.store.deletions(), [(TEAM, {**DELETING, **named})])
        self.assertTrue(self.store.settle(TEAM, EVENT))
        self.store.delete_all()
        self.assertFalse((self.root / "state" / "index.json").exists())

    def test_an_expired_sign_in_deleted_as_it_is_read_is_told(self):
        told = []
        self.store.expired = lambda *expired: told.append(expired)
        self.save()
        self.now += sign_ins.IDLE_SECONDS
        self.assertIsNone(self.current())
        self.save()
        self.now += sign_ins.IDLE_SECONDS
        self.assertEqual(self.checkout().generation, 0)
        self.assertEqual(told, [(TEAM, 1, INCARNATION)] * 2)
        # An expired Sign-in whose deletion is not proven is not told gone, and keeps a new checkout out.
        self.store.delete_team(TEAM)
        self.save()
        self.now += sign_ins.IDLE_SECONDS
        with mock.patch.object(sign_ins.shutil, "rmtree"), self.assertRaises(sign_ins.SignInError) as busy:
            self.checkout()
        self.assertEqual((busy.exception.code, len(told)), ("sign-in-busy", 2))

    def test_a_first_checkout_deletes_cleanly_and_a_starting_team_leaves_a_ready_sign_in(self):
        self.checkout()
        self.assertTrue(self.store.delete_team(TEAM))
        self.assertEqual(self.store._state(), {})
        self.save()
        self.store.reconcile()
        self.assertEqual(self.current()["state"], "ready")

    def test_a_capsule_restores_only_into_the_image_that_wrote_it(self):
        self.save()
        # Another image shows it as needing a person, starts a person's session without it, and refuses an agent.
        self.assertEqual(self.store.current(TEAM, "image@sha256:y")["state"], "needs-person")
        self.assertEqual(self.store.current(TEAM, None)["state"], "needs-person")
        person = self.checkout()
        self.assertIsNone(self.store.capsule(person, "image@sha256:y"))
        self.store.finish(person)
        self.save()
        agent = self.checkout(kind=sign_ins.AGENT)
        with self.assertRaises(sign_ins.SignInError) as refused:
            self.store.capsule(agent, "image@sha256:y")
        self.assertEqual(refused.exception.code, "sign-in-busy")

    def test_a_save_whose_index_write_failed_is_kept_exactly_when_the_index_names_it(self):
        written = sign_ins.SignInStore._write

        def written_then_failed(store, teams):
            written(store, teams)
            raise sign_ins.SignInError("the directory could not be synchronized")

        draft = self.store.draft(self.checkout())
        draft.add(b"kept")
        with mock.patch.object(sign_ins.SignInStore, "_write", written_then_failed):
            saved = draft.commit(["linkedin.com"], False, WRITER, lambda: True)
        self.assertEqual((saved["generation"], self.current()["generation"]), (1, 1))
        self.assertEqual(list(self.store.capsule(self.checkout(), WRITER)), [b"kept"])

    def test_a_save_whose_publication_is_unknown_keeps_its_capsule_for_the_next_start(self):
        self.save()
        checkout = self.checkout()
        draft = self.store.draft(checkout)
        draft.add(b"unknown")
        failed = sign_ins.SignInError("the index could not be written")
        with (
            mock.patch.object(sign_ins.SignInStore, "_write", side_effect=failed),
            mock.patch.object(sign_ins.SignInStore, "_record", side_effect=[self.store._record(TEAM), failed]),
            self.assertRaises(sign_ins.SignInError),
        ):
            draft.commit(["linkedin.com"], False, WRITER, lambda: True)
        draft.discard()
        self.assertIn(f"{checkout.sign_in_id}-2", self.capsules())
        # Named but not proven durable: the capsule stays as well.
        checkout2 = self.store.checkout("trinity", INCARNATION, sign_ins.PERSON)
        draft2 = self.store.draft(checkout2)
        draft2.add(b"named")
        written = sign_ins.SignInStore._write

        def written_then_failed(store, teams):
            written(store, teams)
            raise failed

        synced = sign_ins.private_state.fsync_directory

        def index_unsynced(path):
            if path == self.root / "state":
                raise OSError("unsynced")
            synced(path)

        with (
            mock.patch.object(sign_ins.SignInStore, "_write", written_then_failed),
            mock.patch.object(sign_ins.private_state, "fsync_directory", side_effect=index_unsynced),
            self.assertRaises(sign_ins.SignInError),
        ):
            draft2.commit(["linkedin.com"], False, WRITER, lambda: True)
        draft2.discard()
        self.assertIn(f"{checkout2.sign_in_id}-1", self.capsules())
        self.store.delete_team("trinity")
        # Not named after all: the next start deletes it, and a refused write that named nothing is discarded at once.
        self.store.reconcile()
        self.assertEqual(self.capsules(), [f"{checkout.sign_in_id}-1"])
        checkout = self.checkout()
        draft = self.store.draft(checkout)
        draft.add(b"refused")
        with (
            mock.patch.object(sign_ins.SignInStore, "_write", side_effect=failed),
            self.assertRaises(sign_ins.SignInError),
        ):
            draft.commit(["linkedin.com"], False, WRITER, lambda: True)
        draft.discard()
        self.assertEqual(self.capsules(), [f"{checkout.sign_in_id}-1"])

    def test_a_state_or_record_not_this_stores_own_is_refused(self):
        index = self.root / "state" / "index.json"
        for written in (b"not json", b"[]", b'{"schema": 2, "teams": []}', b'{"schema": 1, "teams": {}}'):
            with self.subTest(written=written):
                index.write_bytes(written)
                index.chmod(0o600)
                with self.assertRaises(sign_ins.SignInError):
                    self.current(TEAM)
        sign_in_id = "a" * 32
        key = self.store._key(create=True)
        aad = sign_ins._record_aad(TEAM, sign_in_id)
        for envelope in (
            {"sign_in_id": sign_in_id},
            {"sign_in_id": "nope", "record": {}},
            {"sign_in_id": sign_in_id, "record": sign_ins.private_state.seal_identified(key, b"not json", aad)},
            {
                "sign_in_id": sign_in_id,
                "record": sign_ins.private_state.seal_identified(key, b'{"team_id": "neo"}', aad),
            },
        ):
            with self.subTest(envelope=str(envelope)[:40]):
                index.write_text(json.dumps({"schema": 2, "teams": {TEAM: envelope}}))
                index.chmod(0o600)
                with self.assertRaises(sign_ins.SignInError):
                    self.current(TEAM)
        index.unlink()
        saved = self.save()
        chunk = next((self.root / "state" / "capsules").rglob("*.chunk"))
        chunk.write_bytes(b"\0" * 40)
        with self.assertRaises(sign_ins.SignInError):
            list(self.store.capsule(self.checkout(), WRITER))
        self.assertEqual(saved["generation"], 1)


if __name__ == "__main__":
    unittest.main()
