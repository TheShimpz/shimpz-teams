"""A Browser is created only while its task may wait for it: by its deadline and until Stop (ADR-0117)."""

import threading
import unittest
from unittest import mock

from test_local_browser_runtime import PUBLISHED, BrowserRuntimeTests, Containers, Images

from local.browser import runtime


class AcquisitionTests(unittest.TestCase):
    setUp = BrowserRuntimeTests.setUp
    make = BrowserRuntimeTests.make

    def test_a_pull_is_waited_for_only_while_the_task_may_wait(self):
        del self.docker.images[PUBLISHED]
        release = threading.Event()
        original = Images.pull

        def stalled(images, repository, tag):
            release.wait(5)
            return original(images, repository, tag)

        started = []

        def counted(images, repository, tag):
            started.append(tag)
            return stalled(images, repository, tag)

        browser = self.make(image=PUBLISHED)
        stop = threading.Event()
        with mock.patch.object(Images, "pull", counted):
            for _attempt in range(3):
                threading.Timer(0.2, stop.set).start()
                with self.assertRaises(runtime.BrowserError):
                    browser.acquire("team_1", "en", wanted=lambda: not stop.is_set())
                stop.clear()
            # Every abandoned wait shared the one pull under way.
            self.assertEqual(len(started), 1)
            with self.assertRaises(runtime.BrowserError):
                browser.acquire("team_1", "en", deadline=0.0)
            release.set()
        self.assertEqual(self.docker.created, [])

    def test_a_task_that_may_no_longer_wait_creates_no_browser(self):
        with self.assertRaises(runtime.BrowserError):
            self.runtime.acquire("team_1", "en", wanted=lambda: False)
        self.assertEqual(self.docker.created, [])

    def test_a_stop_while_the_browser_is_created_never_starts_chromium(self):
        stopped = []
        original = Containers.create

        def creating(containers, **options):
            stopped.append(True)
            return original(containers, **options)

        with mock.patch.object(Containers, "create", creating), self.assertRaises(runtime.BrowserError):
            self.runtime.acquire("team_1", "en", wanted=lambda: not stopped)
        self.assertEqual(len(self.docker.created), len(self.docker.removed))


if __name__ == "__main__":
    unittest.main()
