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

    def acquire(self, team_id, language, *, mode, url):
        self.events.append(("acquire", team_id, language, mode, url))
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
        for patcher in (
            mock.patch.object(local_sign_in, "REST_SECONDS", 0),
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
        ):
            patcher.start()
            self.addCleanup(patcher.stop)
        self.addCleanup(self.sessions.end_all)

    def fake_cua(self, stream):
        session = mock.Mock()
        if isinstance(self.runtime.windows, Exception):
            session.chromium_windows.side_effect = self.runtime.windows
        else:
            session.chromium_windows.return_value = self.runtime.windows
        return session

    def start(self):
        return self.sessions.start(TEAM, INCARNATION, URL, "pt")

    def save(self, started, label="Work", incarnation=INCARNATION):
        return self.sessions.save(TEAM, started["sign_in_session_id"], label, lambda: incarnation)

    def test_a_session_runs_a_persons_browser_watched_through_its_own_view(self):
        started = self.start()
        self.assertEqual(started["site"], "www.linkedin.com")
        self.assertEqual(self.runtime.events, [("acquire", TEAM, "pt-BR", runtime.PERSON, URL)])
        self.assertEqual(self.service.open_view(TEAM), started["view_id"])
        with self.assertRaises(local_sign_in.SignInSessionError) as busy:
            self.start()
        self.assertEqual(busy.exception.code, "browser-busy")
        self.assertEqual(self.runtime.events[-1], ("release",))

    def test_saving_keeps_the_profile_as_a_sign_in_and_releases_the_browser(self):
        started = self.start()
        saved = self.save(started)
        self.assertEqual((saved["site"], saved["label"]), ("www.linkedin.com", "Work"))
        self.assertEqual(self.store.list(TEAM), [saved])
        self.assertEqual(b"".join(self.store.capsule(TEAM, saved["sign_in_id"])), self.runtime.capsule)
        self.assertEqual(self.runtime.events[-2:], [("stop",), ("release",)])
        with self.assertRaises(runtime.BrowserError):
            self.service.open_view(TEAM)

    def test_more_than_one_window_is_refused_and_the_session_goes_on(self):
        started = self.start()
        self.runtime.windows = 2
        with self.assertRaises(local_sign_in.SignInSessionError) as refused:
            self.save(started)
        self.assertEqual(refused.exception.code, "sign-in-windows")
        self.assertEqual(self.service.open_view(TEAM), started["view_id"])

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
                self.assertEqual(self.store.list(TEAM), [])
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
        self.start()
        self.sessions.end_team(TEAM)
        self.sessions.end_team(TEAM)
        self.assertEqual(self.runtime.events[-1], ("release",))
        with self.assertRaises(runtime.BrowserError):
            self.service.open_view(TEAM)

    def test_a_session_ends_at_its_limit_or_left_alone(self):
        for limit in ("SESSION_SECONDS", "IDLE_SECONDS"):
            with (
                self.subTest(limit=limit),
                mock.patch.object(local_sign_in, limit, 0.0),
                mock.patch.object(local_sign_in, "WATCH_SECONDS", 0.01),
            ):
                started = self.start()
                deadline = time.monotonic() + 5
                while time.monotonic() < deadline and self.runtime.events[-1] != ("release",):
                    time.sleep(0.01)
                self.assertEqual(self.runtime.events[-1], ("release",))
                with self.assertRaises(local_sign_in.SignInSessionError):
                    self.sessions.cancel(TEAM, started["sign_in_session_id"])

    def test_only_an_admissible_url_or_label_is_taken(self):
        with self.assertRaises(local_sign_in.SignInSessionError) as url:
            self.sessions.start(TEAM, INCARNATION, "http://www.linkedin.com/", "en")
        self.assertEqual(url.exception.code, "sign-in-url-invalid")
        started = self.start()
        with self.assertRaises(local_sign_in.SignInSessionError) as label:
            self.save(started, label="")
        self.assertEqual(label.exception.code, "sign-in-label-invalid")
        self.assertEqual(threading.active_count() > 0, True)

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
        self.sessions.cancel(TEAM, started["sign_in_session_id"])
        self.assertIn("release-failed", self.audit[-1][1]["detail"])

    def test_the_watch_waits_while_the_person_works_and_yields_to_whoever_ended_the_session(self):
        with mock.patch.object(local_sign_in, "WATCH_SECONDS", 0.01):
            started = self.start()
            time.sleep(0.05)
            # Working within both limits, the session goes on.
            self.assertEqual(self.service.open_view(TEAM), started["view_id"])
            self.sessions.cancel(TEAM, started["sign_in_session_id"])
        with (
            mock.patch.object(local_sign_in, "WATCH_SECONDS", 0.01),
            mock.patch.object(local_sign_in, "SESSION_SECONDS", 0.0),
            mock.patch.object(
                local_sign_in.SignInSessions,
                "_take",
                side_effect=local_sign_in.SignInSessionError("sign-in-not-running"),
            ),
        ):
            self.start()
            time.sleep(0.05)
            # Someone else ended it first: the watch leaves it to them, and so does the Team's end.
            self.sessions.end_team(TEAM)
        self.assertNotEqual(self.audit[-1][1]["detail"].split(":")[0], "expired")

    def test_dropping_another_browsers_view_keeps_the_running_one(self):
        started = self.start()
        self.service.drop(TEAM, object())
        self.assertEqual(self.service.open_view(TEAM), started["view_id"])


if __name__ == "__main__":
    unittest.main()
