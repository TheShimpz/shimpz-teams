"""A person's sign-in session: on the Space's Browser, kept as a Sign-in only when it may be, always released."""

import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from local.browser import runtime
from local.browser import service as local_service
from local.browser import sign_in as local_sign_in
from local.browser import sign_ins as sign_in_store
from local.browser.credentials import Credentials
from tests.test_local_browser_sign_ins import _archive, _cookie_store

TEAM = "neo"
INCARNATION = "n" * 64
URL = "https://www.linkedin.com/login"
ORIGIN = "https://admin.example"
GENERATION = runtime.Generation("egress", "2026-10-19T00:00:00Z", "f" * 32)


class FakeRuntime:
    image = "localhost/shimpz-browser@sha256:" + "e" * 64

    def __init__(self):
        self.events = []
        self.windows = 1
        self.capsule = _archive([("Default", None), ("Default/Cookies", _cookie_store(".linkedin.com"))])
        self.stop_error = None
        self.acquire_error = None
        self.stream_error = None
        self.release_error = None
        # What each background recovery answers in turn; True once the list is spent.
        self.recovers = []

    def acquire(self, team_id, language, *, mode, url, capsule=None):
        self.events.append(("acquire", team_id, language, mode, url))
        self.restored = None if capsule is None else b"".join(capsule)
        if self.acquire_error is not None:
            raise self.acquire_error
        return runtime.Lease(team_id, "container", "172.30.0.3", GENERATION, mode, Credentials.draw())

    def open_relay(self, lease):
        raise AssertionError("no relay in these tests")

    def exited(self, exec_id, deadline):
        return True

    def open_stream(self, lease, deadline, wanted):
        if self.stream_error is not None:
            raise self.stream_error
        return "exec-cua", mock.Mock()

    def stop(self, lease):
        self.events.append(("stop",))
        if self.stop_error is not None:
            raise self.stop_error

    def export(self, lease, sink):
        for start in range(0, len(self.capsule), 1000):
            sink(self.capsule[start : start + 1000])
        return len(self.capsule)

    def release(self, lease):
        self.events.append(("release",))
        if self.release_error is not None:
            raise self.release_error

    def recover(self):
        self.events.append(("recover",))
        return self.recovers.pop(0) if self.recovers else True

    def startup(self):
        return True


class SignInSessionTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        root = Path(directory.name)
        (root / "state").mkdir(mode=0o700)
        (root / "key").mkdir(mode=0o700)
        self.store = sign_in_store.SignInStore(root / "state", root / "key" / "aes256.key")
        self.runtime = FakeRuntime()
        self.service = local_service.BrowserService(self.runtime, storage=None)
        self.sessions = local_sign_in.SignInSessions(self.service, self.runtime, self.store)
        self.audit = []
        self.activity = []
        self.recorded = []
        for patcher in (
            mock.patch.object(local_sign_in, "REST_SECONDS", 0),
            mock.patch.object(local_sign_in, "RECOVER_SECONDS", 0),
            mock.patch.object(
                local_sign_in.local_audit,
                "record_request",
                side_effect=lambda operation, **fields: self.audit.append((operation, fields)),
            ),
            mock.patch.object(
                local_sign_in.local_audit,
                "record",
                side_effect=lambda operation, **fields: self.audit.append((operation, fields)),
            ),
            mock.patch.object(local_sign_in.cua, "CuaSession", self.fake_cua),
            mock.patch.object(
                local_sign_in.local_audit,
                "record_activity",
                side_effect=self._record_activity,
            ),
            mock.patch.object(
                local_sign_in.local_audit, "request_principal", return_value=local_sign_in.local_audit.TEAM
            ),
            mock.patch.object(local_sign_in.local_audit, "flush"),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)
        self.addCleanup(self.sessions.end_all)
        # Background recoveries finish while their audit is still this test's (cleanups run last-added first).
        self.addCleanup(self._join_recoveries)

    def _record_activity(self, operation, **fields):
        self.recorded.append((operation, fields["principal"], fields["activity"]))
        self.activity.append((operation, fields["activity"]["phase"]))

    def _join_recoveries(self):
        for thread in threading.enumerate():
            if thread.name in {"browser-sign-in-recover", "browser-sign-in-delete"}:
                thread.join(5)

    def until(self, event, seconds=5.0):
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline and event not in self.activity:
            time.sleep(0.01)
        self.assertIn(event, self.activity)

    def fake_cua(self, stream):
        session = mock.Mock()
        windows = self.runtime.windows
        # A list answers each count in turn.
        if isinstance(windows, list):
            windows = windows.pop(0)
        if isinstance(windows, Exception):
            session.chromium_windows.side_effect = windows
        else:
            session.chromium_windows.return_value = windows
        return session

    def start(self, incarnation=INCARNATION):
        return self.sessions.start(TEAM, lambda: incarnation, URL, "pt", ORIGIN)

    def save(self, started, incarnation=INCARNATION):
        return self.sessions.save(TEAM, started["sign_in_session_id"], lambda: incarnation, ORIGIN)["sign_in"]

    def held(self, started) -> bool:
        """Whether the Team still holds the session's Browser through its view."""
        try:
            self.service._view(TEAM, started["view_id"])
        except runtime.BrowserError:
            return False
        return True

    def test_a_session_runs_a_persons_browser_through_a_view_no_one_can_watch(self):
        started = self.start()
        self.assertEqual(started["site"], "www.linkedin.com")
        self.assertEqual(self.runtime.events, [("acquire", TEAM, "pt-BR", runtime.PERSON, URL)])
        self.assertTrue(self.held(started))
        # The chat's watch never finds a sign-in session's screen.
        with self.assertRaises(runtime.BrowserError) as unwatched:
            self.service.open_view(TEAM)
        self.assertEqual(unwatched.exception.code, "browser-not-running")
        with self.assertRaises(local_sign_in.SignInSessionError) as busy:
            self.start()
        # The Team's Sign-in is checked out to the running session.
        self.assertEqual(busy.exception.code, "sign-in-busy")
        # Refused before any Browser was asked for.
        self.assertEqual([event[0] for event in self.runtime.events], ["acquire"])

    def test_a_session_whose_team_is_deleted_or_reset_while_it_starts_never_runs(self):
        with self.assertRaises(local_sign_in.SignInSessionError) as gone:
            self.sessions.start(TEAM, lambda: None, URL, "pt", ORIGIN)
        self.assertEqual(gone.exception.code, "team-not-found")
        for end in (lambda: self.sessions.end_team(TEAM), self.sessions.end_all, None):
            plain = self.runtime.acquire
            incarnations = iter((INCARNATION, INCARNATION if end else "after deletion"))

            def acquiring(*args, end=end, plain=plain, **kwargs):
                if end is not None:
                    end()
                return plain(*args, **kwargs)

            with (
                self.subTest(end=end),
                mock.patch.object(self.runtime, "acquire", side_effect=acquiring),
                self.assertRaises(local_sign_in.SignInSessionError) as refused,
            ):
                self.sessions.start(TEAM, lambda incarnations=incarnations: next(incarnations), URL, "pt", ORIGIN)
            self.assertEqual(refused.exception.code, "sign-in-not-running")
            # Released once, by the start alone, and never left registered for a later Cancel to release again.
            self.assertEqual(self.runtime.events.count(("release",)), 1)
            self.assertNotIn(TEAM, self.service._running)
            self.assertNotIn(TEAM, self.sessions._sessions)
            self.runtime.events.clear()

    def test_a_start_whose_session_was_cancelled_as_it_was_rejected_is_released_once(self):
        asked = []

        def incarnation():
            asked.append(1)
            if len(asked) == 2:
                # The Team's end took the registered session just before the start looked again.
                self.sessions.end_team(TEAM)
                return "after deletion"
            return INCARNATION

        with self.assertRaises(local_sign_in.SignInSessionError):
            self.sessions.start(TEAM, incarnation, URL, "pt", ORIGIN)
        self.assertEqual(self.runtime.events.count(("release",)), 1)
        self.assertNotIn(TEAM, self.sessions._sessions)

    def test_saving_keeps_the_profile_as_a_sign_in_and_releases_the_browser(self):
        started = self.start()
        saved = self.save(started)
        self.assertEqual((saved["state"], saved["generation"], saved["sites"]), ("ready", 1, ["linkedin.com"]))
        self.assertEqual(self.sessions.current(TEAM), saved)
        self.assertIsNone(self.runtime.restored)
        self.assertEqual(self.runtime.events[-2:], [("stop",), ("release",)])
        self.assertFalse(self.held(started))
        self.assertEqual(
            self.activity,
            [
                ("browser-sign-in-start", "requested"),
                ("browser-sign-in-start", "completed"),
                ("browser-sign-in-save", "requested"),
                ("browser-sign-in-save", "completed"),
            ],
        )
        # The next session restores what was saved, and its Save writes the next generation.
        again = self.start()
        self.assertEqual(self.runtime.restored, self.runtime.capsule)
        self.assertEqual(self.save(again)["generation"], 2)
        with self.assertRaises(local_sign_in.SignInSessionError) as saved_already:
            self.sessions.cancel(TEAM, started["sign_in_session_id"])
        self.assertEqual(saved_already.exception.code, "sign-in-not-running")

    def test_more_than_one_window_is_refused_and_the_session_goes_on(self):
        started = self.start()
        self.runtime.windows = 2
        with self.assertRaises(local_sign_in.SignInSessionError) as refused:
            self.save(started)
        self.assertEqual(refused.exception.code, "sign-in-windows")
        self.assertTrue(self.held(started))
        # A window the person opened as the Save began is found once their control ended: nothing is kept.
        self.runtime.windows = [1, 2]
        with self.assertRaises(local_sign_in.SignInSessionError) as late:
            self.save(started)
        self.assertEqual(late.exception.code, "sign-in-unsaved")
        self.assertEqual(self.sessions.current(TEAM), None)

    def test_a_session_ended_as_its_save_begins_or_before_it_commits_keeps_nothing(self):
        started = self.start()
        counted = self.sessions._windows

        def cancelled_while_counting(lease):
            self.sessions.cancel(TEAM, started["sign_in_session_id"])
            return counted(lease)

        with (
            mock.patch.object(self.sessions, "_windows", side_effect=cancelled_while_counting),
            self.assertRaises(local_sign_in.SignInSessionError) as ended,
        ):
            self.save(started)
        self.assertEqual(ended.exception.code, "sign-in-not-running")
        started = self.start()
        exported = self.runtime.export
        cancelling = []

        def cancelled_while_exporting(lease, sink):
            size = exported(lease, sink)
            # The person cancels from elsewhere; that Cancel answers once this Save removed the Browser.
            cancel = threading.Thread(target=self.sessions.cancel, args=(TEAM, started["sign_in_session_id"]))
            cancelling.append(cancel)
            cancel.start()
            deadline = time.monotonic() + 5
            while not self.sessions._sessions[TEAM].cancelled and time.monotonic() < deadline:
                time.sleep(0.01)
            return size

        with (
            mock.patch.object(self.runtime, "export", side_effect=cancelled_while_exporting),
            self.assertRaises(local_sign_in.SignInSessionError) as late,
        ):
            self.save(started)
        cancelling[0].join(5)
        self.assertFalse(cancelling[0].is_alive())
        self.assertEqual(late.exception.code, "sign-in-unsaved")
        self.assertEqual(self.sessions.current(TEAM), None)
        self.assertEqual(self.runtime.events[-1], ("release",))

    def test_a_save_waits_for_the_persons_control_to_end_released_or_keeps_nothing(self):
        started = self.start()
        with (
            mock.patch.object(local_sign_in.local_view.View, "quiesce", return_value=False),
            self.assertRaises(local_sign_in.SignInSessionError) as unproven,
        ):
            self.save(started)
        self.assertEqual(unproven.exception.code, "sign-in-unsaved")
        self.assertNotIn(("stop",), self.runtime.events)
        self.assertEqual(self.runtime.events[-1], ("release",))

    def test_cancel_or_the_teams_end_during_a_save_keeps_nothing(self):
        for end in ("cancel", "team", "limit"):
            with self.subTest(end=end), mock.patch.object(local_sign_in, "REST_SECONDS", 5):
                started = self.start()
                outcome = {}

                def saving(started=started, outcome=outcome):
                    try:
                        outcome["saved"] = self.save(started)
                    except local_sign_in.SignInSessionError as exc:
                        outcome["code"] = exc.code

                worker = threading.Thread(target=saving)
                begun = time.monotonic()
                worker.start()
                deadline = time.monotonic() + 5
                while not self.sessions._sessions[TEAM].saving and time.monotonic() < deadline:
                    time.sleep(0.01)
                if end == "cancel":
                    self.sessions.cancel(TEAM, started["sign_in_session_id"])
                elif end == "team":
                    self.sessions.end_team(TEAM)
                else:
                    with (
                        mock.patch.object(local_sign_in, "SESSION_SECONDS", 0.0),
                        mock.patch.object(local_sign_in, "WATCH_SECONDS", 0.01),
                    ):
                        self.sessions._watch(self.sessions._sessions[TEAM])
                worker.join(5)
                self.assertLess(time.monotonic() - begun, 4)
                self.assertEqual(outcome, {"code": "sign-in-not-running"})
                self.assertEqual(self.sessions.current(TEAM), None)
                self.assertNotIn(("stop",), self.runtime.events[-2:])
                self.assertEqual(self.runtime.events[-1], ("release",))
                self.runtime.events.clear()
        # A Cancel during a Save whose Browser could not be removed is no cancelled session.
        started = self.start()
        self.runtime.release_error = runtime.BrowserError("browser-lost")
        outcome = {}
        with mock.patch.object(local_sign_in, "REST_SECONDS", 5):
            worker = threading.Thread(target=lambda: outcome.setdefault("error", self._save_error(started)))
            worker.start()
            deadline = time.monotonic() + 5
            while not self.sessions._sessions[TEAM].saving and time.monotonic() < deadline:
                time.sleep(0.01)
            with self.assertRaises(local_sign_in.SignInSessionError) as kept:
                self.sessions.cancel(TEAM, started["sign_in_session_id"])
            worker.join(5)
        self.assertEqual(kept.exception.code, "browser-unavailable")
        self.assertEqual(outcome["error"], "sign-in-not-running")
        self.runtime.release_error = None

    def _save_error(self, started) -> str:
        try:
            self.save(started)
        except local_sign_in.SignInSessionError as exc:
            return exc.code
        return ""

    def test_nothing_is_kept_when_the_profile_or_the_stop_or_the_team_does_not_allow_it(self):
        cases = (
            ("identity", "sign-in-identity-provider", INCARNATION),
            ("stop", "sign-in-unsaved", INCARNATION),
            ("capsule", "sign-in-unsaved", INCARNATION),
            ("team", "sign-in-unsaved", "another incarnation"),
        )
        for case, code, incarnation in cases:
            with self.subTest(case=case):
                self.runtime.capsule = _archive(
                    [("Default", None), ("Default/Cookies", _cookie_store(".linkedin.com"))]
                )
                self.runtime.stop_error = None
                if case == "identity":
                    self.runtime.capsule = _archive(
                        [("Default", None), ("Default/Cookies", _cookie_store("accounts.google.com"))]
                    )
                elif case == "stop":
                    self.runtime.stop_error = runtime.BrowserError("browser-lost")
                elif case == "capsule":
                    self.runtime.capsule = self.runtime.capsule[:-512]
                started = self.start()
                with self.assertRaises(local_sign_in.SignInSessionError) as refused:
                    self.save(started, incarnation=incarnation)
                self.assertEqual(refused.exception.code, code)
                self.assertEqual(self.sessions.current(TEAM), None)
                self.assertEqual(self.runtime.events[-1], ("release",))

    def test_cancel_and_the_teams_end_release_the_browser_and_keep_nothing(self):
        started = self.start()
        self.sessions.cancel(TEAM, started["sign_in_session_id"])
        self.assertEqual(self.runtime.events[-1], ("release",))
        for call in (
            lambda: self.sessions.cancel(TEAM, started["sign_in_session_id"]),
            lambda: self.save(started),
        ):
            with self.assertRaises(local_sign_in.SignInSessionError) as gone:
                call()
            self.assertEqual(gone.exception.code, "sign-in-not-running")
        again = self.start()
        self.sessions.end_team(TEAM)
        self.sessions.end_team(TEAM)
        self.assertEqual(self.runtime.events[-1], ("release",))
        self.assertFalse(self.held(again))

    def test_a_session_ends_at_its_limit_or_left_alone(self):
        for limit in ("SESSION_SECONDS", "IDLE_SECONDS"):
            with (
                self.subTest(limit=limit),
                mock.patch.object(local_sign_in, limit, 0.0),
                mock.patch.object(local_sign_in, "WATCH_SECONDS", 0.01),
            ):
                self.activity.clear()
                started = self.start()
                deadline = time.monotonic() + 5
                while time.monotonic() < deadline and ("browser-sign-in-cancel", "completed") not in self.activity:
                    time.sleep(0.01)
                self.assertEqual(self.runtime.events[-1], ("release",))
                with self.assertRaises(local_sign_in.SignInSessionError):
                    self.sessions.cancel(TEAM, started["sign_in_session_id"])

    def test_only_an_admissible_url_is_taken(self):
        with self.assertRaises(local_sign_in.SignInSessionError) as url:
            self.sessions.start(TEAM, lambda: INCARNATION, "http://www.linkedin.com/", "en", ORIGIN)
        self.assertEqual(url.exception.code, "sign-in-url-invalid")

    def test_a_browser_driver_or_release_that_fails_is_answered_and_nothing_kept(self):
        self.runtime.acquire_error = runtime.BrowserError("browser-unavailable")
        with self.assertRaises(local_sign_in.SignInSessionError) as unavailable:
            self.start()
        self.assertEqual(unavailable.exception.code, "browser-unavailable")
        self.runtime.acquire_error = None
        for failure in ("stream", "driver"):
            with self.subTest(failure=failure):
                started = self.start()
                self.runtime.stream_error = runtime.BrowserError("browser-lost") if failure == "stream" else None
                self.runtime.windows = (
                    local_sign_in.cua.CuaError("browser-window-unavailable") if failure == "driver" else 1
                )
                with self.assertRaises(local_sign_in.SignInSessionError) as unsaved:
                    self.save(started)
                self.assertEqual(unsaved.exception.code, "sign-in-unsaved")
                self.sessions.cancel(TEAM, started["sign_in_session_id"])
        self.runtime.stream_error, self.runtime.windows = None, 1
        self.runtime.release_error = runtime.BrowserError("browser-lost")
        started = self.start()
        # A Browser whose removal failed is no cancelled session.
        with self.assertRaises(local_sign_in.SignInSessionError) as kept:
            self.sessions.cancel(TEAM, started["sign_in_session_id"])
        self.assertEqual(kept.exception.code, "browser-unavailable")
        self.assertTrue(any("release-failed" in entry[1]["detail"] for entry in self.audit))
        # The Team's end still goes on, its Browser removed and proven absent afterwards.
        self.runtime.release_error = None
        self.start()
        self.runtime.release_error = runtime.BrowserError("browser-lost")
        self.sessions.end_team(TEAM)
        self.assertNotIn(TEAM, self.sessions._sessions)
        self.runtime.release_error = None

    def test_the_watch_waits_while_the_person_works_and_yields_to_whoever_ended_the_session(self):
        with mock.patch.object(local_sign_in, "WATCH_SECONDS", 0.01):
            started = self.start()
            time.sleep(0.05)
            # Working within both limits, the session goes on.
            self.assertTrue(self.held(started))
            self.sessions.cancel(TEAM, started["sign_in_session_id"])
        with (
            mock.patch.object(local_sign_in, "WATCH_SECONDS", 0.01),
            mock.patch.object(local_sign_in, "SESSION_SECONDS", 0.0),
            mock.patch.object(
                local_sign_in.SignInSessions,
                "_end",
                side_effect=local_sign_in.SignInSessionError("sign-in-not-running"),
            ),
        ):
            self.start()
            time.sleep(0.05)
            # Someone else ended it first: the watch leaves it to them, and so does the Team's end.
            self.sessions.end_team(TEAM)
        self.assertNotEqual(self.audit[-1][1]["detail"].split(":")[0], "cancelled")
        self.sessions._sessions.clear()
        self.service._running.clear()

    def test_a_browser_whose_removal_failed_is_cleaned_again_until_its_absence_is_proven(self):
        for recovers, outcome in (([False, True], "recovered"),):
            with self.subTest(outcome=outcome):
                self.runtime.recovers = list(recovers)
                self.runtime.release_error = runtime.BrowserError("browser-lost")
                started = self.start()
                with self.assertRaises(local_sign_in.SignInSessionError):
                    self.sessions.cancel(TEAM, started["sign_in_session_id"])
                self.runtime.release_error = None
                deadline = time.monotonic() + 5
                while time.monotonic() < deadline and not any(
                    entry[1]["detail"].startswith(outcome) for entry in self.audit
                ):
                    time.sleep(0.01)
                self.assertEqual(self.runtime.events.count(("recover",)), 2)
                self.assertTrue(any(entry[1]["detail"].startswith(outcome) for entry in self.audit))
                self.runtime.events.clear()

    def test_a_session_whose_screen_is_lost_ends(self):
        with mock.patch.object(local_sign_in, "WATCH_SECONDS", 0.01):
            started = self.start()
            self.sessions._sessions[TEAM].view._unavailable = True
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline and ("browser-sign-in-cancel", "completed") not in self.activity:
                time.sleep(0.01)
        self.assertNotIn(TEAM, self.sessions._sessions)
        self.assertFalse(self.held(started))
        self.assertTrue(any(entry[1]["detail"].startswith("lost") for entry in self.audit))

    def confirmed(self, saved, factor="totp", **changes):
        return {
            "sign_in_id": saved["sign_in_id"],
            "generation": saved["generation"],
            "team_incarnation": INCARNATION,
            "factor": factor,
            "origin": ORIGIN,
            **changes,
        }

    def test_a_browser_the_team_already_holds_refuses_the_session_and_keeps_nothing(self):
        other = runtime.Lease(TEAM, "other", "172.30.0.4", GENERATION, runtime.AGENT, None)
        self.service.hold(TEAM, "task", other, mock.Mock())
        with self.assertRaises(local_sign_in.SignInSessionError) as busy:
            self.start()
        self.assertEqual(busy.exception.code, "browser-busy")
        self.assertEqual(self.runtime.events[-1], ("release",))
        self.assertEqual(self.store._state(), {})
        self.service._running.clear()

    def test_a_cancel_after_a_restore_leaves_the_sign_in_needing_a_person(self):
        self.save(self.start())
        started = self.start()
        self.sessions.cancel(TEAM, started["sign_in_session_id"])
        self.assertEqual(self.sessions.current(TEAM)["state"], "needs-person")
        # A person's Save reconciles it.
        self.assertEqual(self.save(self.start())["state"], "ready")

    def test_deletion_takes_the_exact_confirmed_sign_in_and_ends_a_session_using_it(self):
        saved = self.save(self.start())
        for changes in ({"generation": 2}, {"team_incarnation": "m" * 64}, {"sign_in_id": "b" * 32}):
            with self.subTest(changes=changes), self.assertRaises(local_sign_in.SignInSessionError) as changed:
                self.sessions.delete(TEAM, self.confirmed(saved, **changes))
            self.assertEqual(changed.exception.code, "sign-in-changed")
        started = self.start()
        self.activity.clear()
        self.assertEqual(self.sessions.delete(TEAM, self.confirmed(saved, factor="passkey")), "deleted")
        self.assertIsNone(self.sessions.current(TEAM))
        self.assertFalse(self.held(started))
        self.assertEqual(self.activity[0], ("browser-sign-in-delete", "requested"))
        self.assertEqual(self.activity[-1], ("browser-sign-in-delete", "completed"))
        with self.assertRaises(local_sign_in.SignInSessionError) as absent:
            self.sessions.delete(TEAM, self.confirmed(saved))
        self.assertEqual(absent.exception.code, "sign-in-absent")

    def test_a_deletion_it_cannot_prove_now_is_pending_and_completes_in_the_background(self):
        saved = self.save(self.start())
        attempts = iter((False, sign_in_store.SignInError("held"), False, True))

        def purge(_team, _event):
            outcome = next(attempts)
            if isinstance(outcome, Exception):
                raise outcome
            return outcome

        with (
            mock.patch.object(local_sign_in.local_audit, "human_principal", return_value="s" * 32),
            mock.patch.object(self.store, "purge", side_effect=purge),
        ):
            self.assertEqual(self.sessions.delete(TEAM, self.confirmed(saved)), "pending")
            self.until(("browser-sign-in-delete", "completed"))
        # Team records the completion on its own, naming the Supervisor who confirmed it and how.
        operation, principal, completed = self.recorded[-1]
        self.assertEqual((operation, principal), ("browser-sign-in-delete", local_sign_in.local_audit.TEAM))
        self.assertEqual(
            {
                key: completed[key]
                for key in ("generation", "incarnation", "factor", "initiator_id", "origin", "outcome")
            },
            {
                "generation": 1,
                "incarnation": INCARNATION,
                "factor": "totp",
                "initiator_id": "s" * 32,
                "origin": "team",
                "outcome": "deleted",
            },
        )
        self.assertEqual((self.sessions.current(TEAM), self.store.deletions()), (None, []))

    def test_a_deletion_whose_fence_cannot_be_dropped_yet_is_pending_and_drops_it_later(self):
        saved = self.save(self.start())
        settle = self.store.settle
        attempts = [sign_in_store.SignInError("held")]

        def settling(team_id, event_id):
            if attempts:
                raise attempts.pop(0)
            return settle(team_id, event_id)

        with mock.patch.object(self.store, "settle", side_effect=settling):
            self.assertEqual(self.sessions.delete(TEAM, self.confirmed(saved)), "pending")
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline and self.store.deletions():
                time.sleep(0.01)
        self.assertEqual(self.store.deletions(), [])

    def test_a_deletion_whose_completion_the_journal_cannot_record_stays_pending_until_it_can(self):
        saved = self.save(self.start())
        flushes = [RuntimeError("journal down")] * 2

        def flush():
            if flushes:
                raise flushes.pop(0)

        with mock.patch.object(local_sign_in.local_audit, "flush", side_effect=flush):
            self.assertEqual(self.sessions.delete(TEAM, self.confirmed(saved)), "pending")
            self.until(("browser-sign-in-delete", "completed"))
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline and self.store.deletions():
                time.sleep(0.01)
        self.assertEqual(self.store.deletions(), [])

    def test_a_deletion_while_a_session_starts_is_pending_until_that_browser_is_gone(self):
        saved = self.save(self.start())
        plain, answers = self.runtime.acquire, {}

        def acquiring(*args, **kwargs):
            lease = plain(*args, **kwargs)
            answers["deletion"] = self.sessions.delete(TEAM, self.confirmed(saved))
            return lease

        with (
            mock.patch.object(self.runtime, "acquire", side_effect=acquiring),
            self.assertRaises(local_sign_in.SignInSessionError) as refused,
        ):
            self.start()
        self.assertEqual((refused.exception.code, answers["deletion"]), ("sign-in-not-running", "pending"))
        self.until(("browser-sign-in-delete", "completed"))
        self.assertEqual(self.runtime.events.count(("release",)), 2)
        self.assertNotIn(TEAM, self.sessions._sessions)
        self.assertIsNone(self.sessions.current(TEAM))

    def test_a_deletion_whose_session_browser_could_not_be_removed_is_pending_until_it_is(self):
        saved = self.save(self.start())
        self.start()
        self.runtime.release_error = runtime.BrowserError("browser-lost")
        cleaned = threading.Event()
        recover = self.runtime.recover

        def recovering():
            recover()
            return cleaned.is_set()

        with mock.patch.object(self.runtime, "recover", side_effect=recovering):
            self.assertEqual(self.sessions.delete(TEAM, self.confirmed(saved)), "pending")
            self.runtime.release_error = None
            self.assertNotIn(("browser-sign-in-delete", "completed"), self.activity)
            cleaned.set()
            self.until(("browser-sign-in-delete", "completed"))
        self.assertGreaterEqual(self.runtime.events.count(("recover",)), 1)

    def test_a_deletion_while_a_cancel_still_removes_the_browser_waits_for_that_removal(self):
        saved = self.save(self.start())
        started = self.start()
        releasing, removed = threading.Event(), threading.Event()
        release = self.runtime.release

        def slow_release(lease):
            releasing.set()
            removed.wait(5)
            release(lease)

        with mock.patch.object(self.runtime, "release", side_effect=slow_release):
            cancel = threading.Thread(target=self.sessions.cancel, args=(TEAM, started["sign_in_session_id"]))
            cancel.start()
            releasing.wait(5)
            self.assertEqual(self.sessions.delete(TEAM, self.confirmed(saved)), "pending")
            removed.set()
            cancel.join(5)
            self.until(("browser-sign-in-delete", "completed"))

    def test_every_event_names_its_origin_and_why_a_session_ended(self):
        self.save(self.start())
        requested = [activity for operation, _principal, activity in self.recorded]
        self.assertEqual({activity["origin"] for activity in requested}, {ORIGIN})
        self.assertEqual({activity["incarnation"] for activity in requested}, {INCARNATION})
        with (
            mock.patch.object(local_sign_in.local_audit, "human_principal", return_value="s" * 32),
            mock.patch.object(local_sign_in, "SESSION_SECONDS", 0.0),
            mock.patch.object(local_sign_in, "WATCH_SECONDS", 0.01),
        ):
            self.start()
            self.until(("browser-sign-in-cancel", "completed"))
        operation, principal, ended = self.recorded[-1]
        self.assertEqual(
            (operation, principal, ended["origin"], ended["outcome"], ended["initiator_id"]),
            ("browser-sign-in-cancel", local_sign_in.local_audit.TEAM, "team", "session-limit", "s" * 32),
        )
        # A Sign-in that expired is recorded by Team as it is deleted.
        with mock.patch.object(self.store, "_now", return_value=time.time() + sign_in_store.LIFETIME_SECONDS):
            self.assertIsNone(self.sessions.current(TEAM))
        operation, principal, expired = self.recorded[-1]
        self.assertEqual(
            (operation, principal, expired["origin"], expired["outcome"], expired["generation"]),
            ("browser-sign-in-expire", local_sign_in.local_audit.TEAM, "team", "expired", 1),
        )

    def test_a_restarted_team_completes_and_records_every_deletion_left_pending(self):
        saved = self.save(self.start())
        metadata = {"event_id": "e" * 32, "initiator_id": "s" * 32, "factor": "passkey", "origin": ORIGIN}
        self.store.begin_delete(TEAM, (saved["sign_in_id"], 1, INCARNATION), metadata)
        for proven in (False, True):
            with self.subTest(proven=proven):
                restarted = local_sign_in.SignInSessions(self.service, self.runtime, self.store)
                self.runtime.recovers = [False]
                with mock.patch.object(self.service, "startup", return_value=proven):
                    restarted.startup()
                self.until(("browser-sign-in-delete", "completed"))
                _operation, principal, completed = self.recorded[-1]
                self.assertEqual(
                    (principal, completed["event_id"], completed["initiator_id"], completed["factor"]),
                    (local_sign_in.local_audit.TEAM, "e" * 32, "s" * 32, "passkey"),
                )
                deadline = time.monotonic() + 5
                while time.monotonic() < deadline and self.store.deletions():
                    time.sleep(0.01)
                self.assertEqual(self.store.deletions(), [])
                saved = self.save(self.start())
                self.store.begin_delete(TEAM, (saved["sign_in_id"], 1, INCARNATION), metadata)
                self.activity.clear()
        # Pending deletions the store cannot read yet are read again until it can.
        unreadable = mock.patch.object(
            self.store, "deletions", side_effect=[sign_in_store.SignInError("unreadable"), []]
        )
        with unreadable as deletions:
            local_sign_in.SignInSessions(self.service, self.runtime, self.store).startup()
        self.assertEqual(deletions.call_count, 2)

    def test_a_journal_that_cannot_record_never_stops_a_cancel_or_a_sessions_limit(self):
        failing = mock.patch.object(
            local_sign_in.local_audit, "record_request", side_effect=RuntimeError("journal down")
        )
        started = self.start()
        with failing:
            self.sessions.cancel(TEAM, started["sign_in_session_id"])
        self.assertEqual(self.runtime.events[-1], ("release",))
        with (
            mock.patch.object(local_sign_in, "SESSION_SECONDS", 0.0),
            mock.patch.object(local_sign_in, "WATCH_SECONDS", 0.01),
            mock.patch.object(local_sign_in.local_audit, "record", side_effect=RuntimeError("journal down")),
        ):
            self.activity.clear()
            self.start()
            self.until(("browser-sign-in-cancel", "completed"))
        self.assertEqual(self.runtime.events[-1], ("release",))

    def test_a_start_whose_completion_the_journal_cannot_record_ends_at_once(self):
        flushes = iter((None, RuntimeError("journal down")))

        def flush():
            outcome = next(flushes, None)
            if outcome is not None:
                raise outcome

        with (
            mock.patch.object(local_sign_in.local_audit, "flush", side_effect=flush),
            self.assertRaises(local_sign_in.SignInSessionError) as refused,
        ):
            self.start()
        self.assertEqual(refused.exception.code, "browser-unavailable")
        self.assertEqual(self.runtime.events[-1], ("release",))
        self.assertNotIn(TEAM, self.sessions._sessions)
        self.assertEqual(self.store._state(), {})

    def test_a_start_the_store_refuses_or_another_start_holds_is_refused(self):
        saved = self.save(self.start())
        self.store.begin_delete(
            TEAM,
            (saved["sign_in_id"], 1, INCARNATION),
            {"event_id": "e" * 32, "initiator_id": "", "factor": "totp", "origin": ORIGIN},
        )
        with self.assertRaises(local_sign_in.SignInSessionError) as deleting:
            self.start()
        self.assertEqual(deleting.exception.code, "sign-in-busy")
        self.sessions._starting.add(TEAM)
        with self.assertRaises(local_sign_in.SignInSessionError) as starting:
            self.start()
        self.assertEqual(starting.exception.code, "sign-in-busy")
        self.sessions._starting.clear()

    def test_nothing_delegating_starts_or_saves_unless_its_activity_is_recorded_first(self):
        started = self.start()
        with mock.patch.object(local_sign_in.local_audit, "flush", side_effect=RuntimeError("journal down")):
            with self.assertRaises(local_sign_in.SignInSessionError) as save_refused:
                self.save(started)
            self.sessions.cancel(TEAM, started["sign_in_session_id"])
            with self.assertRaises(local_sign_in.SignInSessionError) as start_refused:
                self.start()
        self.assertEqual((save_refused.exception.code, start_refused.exception.code), ("browser-unavailable",) * 2)
        self.assertIsNone(self.sessions.current(TEAM))
        # A completion the journal could not record after the commit is answered as committed, marked unrecorded.
        started = self.start()
        flushes = iter((None, RuntimeError("journal down")))

        def flush():
            outcome = next(flushes)
            if outcome is not None:
                raise outcome

        with mock.patch.object(local_sign_in.local_audit, "flush", side_effect=flush):
            answer = self.sessions.save(TEAM, started["sign_in_session_id"], lambda: INCARNATION, ORIGIN)
        self.assertEqual((answer["recorded"], answer["sign_in"]["generation"]), (False, 1))

    def test_dropping_another_browsers_view_keeps_the_running_one(self):
        started = self.start()
        self.service.drop(TEAM, object())
        self.assertTrue(self.held(started))


if __name__ == "__main__":
    unittest.main()
