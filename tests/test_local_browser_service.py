"""One chat turn's Browser task on the Local Team: acquire, run, ingest, always release, and audit."""

import json
import unittest
from unittest import mock

from browser import cua, ingest, task
from inference import client as brain_runtime_client
from inference.errors import BrainRuntimeError
from local import audit as local_audit
from local.browser import runtime, service
from storage import files as team_storage

LEASE = runtime.Lease("team_1", "c" * 64, "172.30.0.9", ("e" * 64, "2026-10-17T00:00:01Z"))
CONTEXT = brain_runtime_client.RuntimeContext("thread", "Team", (), "openai", "gpt-6-luna", "test-key")
REQUEST = brain_runtime_client.BrowserRequest("interrupt-1", "Veja o prazo do edital.")


class FakeRuntime:
    def __init__(self, *, acquire=None, listing=None, contents=None, release=None, files_error=None):
        self.acquire_error, self.release_error, self.files_error = acquire, release, files_error
        self.listing = listing or {"files": [], "refused": [], "skipped": 0}
        self.contents = contents or {}
        self.available = True
        self.events = []

    def acquire(self, team_id, language):
        self.events.append(("acquire", team_id, language))
        if self.acquire_error:
            raise runtime.BrowserError(self.acquire_error)
        return LEASE

    def open_stream(self, lease):
        self.events.append(("stream", lease.container_id))
        return "stream"

    def files(self, lease):
        if self.files_error:
            raise self.files_error
        return self.listing

    def read(self, lease, name, size):
        if self.contents.get(name) is None:
            raise runtime.BrowserError("browser-lost")
        return self.contents[name]

    def release(self, lease):
        self.events.append(("release", lease.container_id))
        if self.release_error:
            raise runtime.BrowserError(self.release_error)

    def startup(self):
        return True

    def remove_team(self, team_id):
        self.events.append(("remove", team_id))

    def clean(self):
        self.events.append(("clean",))


class FakeStorage:
    def __init__(self):
        self.stored = []

    def put(self, team_id, name, data, media_type):
        self.stored.append((team_id, name, media_type))
        return {"id": "f" * 32, "name": name, "media_type": media_type, "size": len(data), "sha256": "x"}


class FakeWeb:
    def __init__(self):
        self.marked = []

    def mark(self, team_id, digest):
        self.marked.append((team_id, digest))


class FakeSession:
    def __init__(self, channel):
        self.channel = channel

    def open(self):
        pass


class BrowserServiceTests(unittest.TestCase):
    def setUp(self):
        self.audit = []
        patcher = mock.patch.object(
            local_audit,
            "record_request",
            side_effect=lambda operation, **fields: self.audit.append((operation, fields)),
        )
        patcher.start()
        self.addCleanup(patcher.stop)
        session = mock.patch.object(service.cua, "CuaSession", FakeSession)
        session.start()
        self.addCleanup(session.stop)
        self.brain = mock.Mock()
        self.turn = service.BrowserTurn("team_1", "pt-BR", self.brain, lambda: False)

    def make(self, fake):
        self.runtime, self.storage, self.web = fake, FakeStorage(), FakeWeb()
        return service.BrowserService(fake, self.storage, self.web)

    def run_with(self, browser_service, outcome):
        with mock.patch.object(service.browser_task.Task, "run", return_value=outcome):
            return browser_service.run(self.turn, CONTEXT, REQUEST)

    def test_a_finished_task_ingests_its_downloads_and_always_releases(self):
        listing = {
            "files": [{"name": "edital.pdf", "size": 9}],
            "refused": [{"name": "x.crdownload", "reason": "incomplete", "size": 1}],
            "skipped": 1,
        }
        browser_service = self.make(FakeRuntime(listing=listing, contents={"edital.pdf": b"%PDF-1.4\n"}))
        result, terminal = self.run_with(browser_service, task.Outcome("done", summary="Prazo: 30/11.", steps=4))
        self.assertEqual(
            result,
            {
                "status": "done",
                "summary": "Prazo: 30/11.",
                "files": [
                    {"name": "edital.pdf", "media_type": "application/pdf", "size": 9, "outcome": "kept"},
                    {"name": "x.crdownload", "outcome": "incomplete"},
                ],
            },
        )
        self.assertEqual(
            terminal,
            {
                "files": [{"id": "f" * 32, "name": "edital.pdf", "media_type": "application/pdf", "size": 9}],
                "lost": [{"name": "x.crdownload", "reason": "incomplete"}],
                "skipped": 1,
            },
        )
        self.assertEqual(
            self.runtime.events,
            [("acquire", "team_1", "pt-BR"), ("stream", LEASE.container_id), ("release", LEASE.container_id)],
        )
        self.assertEqual(len(self.web.marked), 1)
        details = [fields["detail"] for operation, fields in self.audit if operation == "browser-task"]
        self.assertTrue(details[0].startswith("started:container=cccccccccccc:address=172.30.0.9:egress=eeeeeeeeeeee@"))
        self.assertTrue(details[1].startswith("done:steps=4:released=1"))
        self.assertNotIn("edital", json.dumps(self.audit))

    def test_a_stopped_task_ingests_nothing_and_a_needs_person_task_says_why(self):
        browser_service = self.make(
            FakeRuntime(listing={"files": [{"name": "a.pdf", "size": 1}], "refused": [], "skipped": 0})
        )
        result, terminal = self.run_with(browser_service, task.Outcome("stopped", steps=2))
        self.assertEqual(
            (result, terminal), ({"status": "stopped", "files": []}, {"files": [], "lost": [], "skipped": 0})
        )
        result, _ = self.run_with(
            self.make(FakeRuntime()),
            task.Outcome("needs-person", reason="sign-in", sentence="O site pede login.", steps=3),
        )
        self.assertEqual(
            result, {"status": "needs-person", "reason": "sign-in", "sentence": "O site pede login.", "files": []}
        )
        result, _ = self.run_with(self.make(FakeRuntime()), task.Outcome("budget", steps=40))
        self.assertEqual(result, {"status": "failed", "files": []})

    def test_an_unavailable_or_busy_browser_ends_the_task_before_any_container(self):
        for code in ("browser-busy", "browser-unavailable"):
            with self.subTest(code=code):
                browser_service = self.make(FakeRuntime(acquire=code))
                result, terminal = browser_service.run(self.turn, CONTEXT, REQUEST)
                self.assertEqual(result, {"status": "failed", "reason": code, "files": []})
                self.assertEqual(terminal, {"files": [], "lost": [], "skipped": 0})
                self.assertEqual(self.runtime.events, [("acquire", "team_1", "pt-BR")])

    def test_a_lost_browser_and_a_failed_release_are_reported_and_never_leak(self):
        browser_service = self.make(
            FakeRuntime(files_error=runtime.BrowserError("browser-lost"), release="browser-lost")
        )
        result, _terminal = self.run_with(browser_service, task.Outcome("done", summary="x", steps=1))
        self.assertEqual(result, {"status": "failed", "reason": "browser-lost", "files": []})
        detail = [fields["detail"] for operation, fields in self.audit if operation == "browser-task"][-1]
        self.assertTrue(detail.startswith("failed:steps=1:released=0"))
        broken = self.make(FakeRuntime(files_error=ingest.ListingError("bad")))
        self.assertEqual(self.run_with(broken, task.Outcome("done", summary="x", steps=1))[0]["reason"], "browser-lost")
        lost = self.make(FakeRuntime())
        with mock.patch.object(service.browser_task.Task, "run", side_effect=cua.CuaError("tool-failed")):
            self.assertEqual(lost.run(self.turn, CONTEXT, REQUEST)[0]["reason"], "browser-lost")
        self.assertEqual(self.runtime.events[-1], ("release", LEASE.container_id))

    def test_a_read_that_fails_marks_the_rest_lost(self):
        listing = {"files": [{"name": "a.pdf", "size": 9}], "refused": [], "skipped": 0}
        browser_service = self.make(FakeRuntime(listing=listing))
        result, terminal = self.run_with(browser_service, task.Outcome("done", summary="x", steps=1))
        self.assertEqual(terminal["lost"], [{"name": "a.pdf", "reason": "browser-lost"}])
        self.assertEqual(result["files"], [{"name": "a.pdf", "outcome": "browser-lost"}])

    def test_each_step_goes_to_the_brain_with_the_turns_context_and_a_brain_failure_ends_it(self):
        self.brain.browser_step.return_value = ({"kind": "done", "summary": "ok"}, 120)
        browser_service = self.make(FakeRuntime())
        session = mock.Mock()
        session.observe.return_value = cua.Observation("u", "t", "url: u", {})
        with mock.patch.object(service.cua, "CuaSession", return_value=session):
            result, _ = browser_service.run(self.turn, CONTEXT, REQUEST)
        self.assertEqual(result["status"], "done")
        context, payload = self.brain.browser_step.call_args.args
        self.assertIs(context, CONTEXT)
        self.assertEqual((payload["objective"], payload["language"]), ("Veja o prazo do edital.", "pt-BR"))
        self.brain.browser_step.side_effect = BrainRuntimeError("down")
        with mock.patch.object(service.cua, "CuaSession", return_value=session):
            result, _ = browser_service.run(self.turn, CONTEXT, REQUEST)
        self.assertEqual((result["status"], result["reason"]), ("failed", "model-unavailable"))

    def test_the_language_follows_the_turn_and_lifecycle_calls_reach_the_runtime(self):
        self.assertEqual(
            [service.language(locale) for locale in ("pt-BR", "pt", "en", None, "fr")],
            ["pt-BR", "pt-BR", "en", "en", "en"],
        )
        browser_service = self.make(FakeRuntime())
        self.assertTrue(browser_service.available)
        self.assertTrue(browser_service.startup())
        browser_service.remove_team("team_2")
        browser_service.clean()
        self.assertEqual(self.runtime.events, [("remove", "team_2"), ("clean",)])

    def test_storage_failures_become_closed_losses(self):
        listing = {"files": [{"name": "a.pdf", "size": 9}], "refused": [], "skipped": 0}
        browser_service = self.make(FakeRuntime(listing=listing, contents={"a.pdf": b"%PDF-1.4\n"}))
        self.storage.put = mock.Mock(side_effect=team_storage.StorageQuotaError("full"))
        _, terminal = self.run_with(browser_service, task.Outcome("done", summary="x", steps=1))
        self.assertEqual(terminal["lost"], [{"name": "a.pdf", "reason": "quota-exceeded"}])


if __name__ == "__main__":
    unittest.main()
