"""One chat turn's Browser task on the Local Team: acquire, run, ingest, always release, and audit."""

import json
import unittest
from unittest import mock

from browser import cua, ingest, task
from inference import client as brain_runtime_client
from inference.errors import BrainRuntimeError
from local import audit as local_audit
from local.browser import runtime, service
from local.browser import view as local_view
from local.chat import segment as local_chat_segment
from storage import files as team_storage

LEASE = runtime.Lease("team_1", "c" * 64, "172.30.0.9", runtime.Generation("e" * 64, "2026-10-17T00:00:01Z", "9" * 32))
CONTEXT = brain_runtime_client.RuntimeContext("thread", "Team", (), "openai", "gpt-6-luna", "test-key")
STOPPED_TURN = "0" * 32
TURN = "7" * 32
REQUEST = brain_runtime_client.BrowserRequest("interrupt-1", "Veja o prazo do edital.")
# The real Driver session, before each test replaces it with a fake.
DRIVER_SESSION = cua.CuaSession


class FakeRuntime:
    def __init__(self, *, acquire=None, listing=None, contents=None, release=None, files_error=None):
        self.acquire_error, self.release_error, self.files_error = acquire, release, files_error
        self.listing = listing or {"files": [], "refused": [], "skipped": 0}
        self.contents = contents or {}
        self.available = True
        self.kill_error = False
        self.intact_after = True
        self.events = []

    def acquire(self, team_id, language):
        self.events.append(("acquire", team_id, language))
        if self.acquire_error:
            raise runtime.BrowserError(self.acquire_error)
        return LEASE

    def open_stream(self, lease, deadline, wanted):
        self.events.append(("stream", lease.container_id))
        return "exec-mcp", "stream"

    def files(self, lease):
        if self.files_error:
            raise self.files_error
        return self.listing

    def read(self, lease, name, size):
        if self.contents.get(name) is None:
            raise runtime.BrowserError("browser-lost")
        return self.contents[name]

    def intact(self, lease):
        return self.intact_after

    def kill(self, lease):
        self.events.append(("kill", lease.container_id))
        if self.kill_error:
            raise runtime.BrowserError("browser-lost")

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

    def put(self, team_id, name, data, media_type, *, web=False):
        self.stored.append((team_id, name, media_type, web))
        return {"id": "f" * 32, "name": name, "media_type": media_type, "size": len(data), "sha256": "x"}


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
        self.turn = service.BrowserTurn("team_1", "d" * 64, TURN, "pt-BR", self.brain, lambda: False)

    def make(self, fake):
        self.runtime, self.storage = fake, FakeStorage()
        return service.BrowserService(fake, self.storage)

    def run_with(self, browser_service, outcome):
        return self.run_with_turn(browser_service, self.turn, outcome)

    def run_with_turn(self, browser_service, turn, outcome):
        with mock.patch.object(service.browser_task.Task, "run", return_value=outcome):
            return browser_service.run(turn, CONTEXT, REQUEST)

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
        self.assertEqual(self.storage.stored, [("team_1", "edital.pdf", "application/pdf", True)])
        details = [fields["detail"] for operation, fields in self.audit if operation == "browser-task"]
        self.assertEqual(
            details[0],
            "started:incarnation=dddddddddddd:container=cccccccccccc:address=172.30.0.9"
            f":egress=eeeeeeeeeeee@2026-10-17T00:00:01Z:generation={'9' * 32}",
        )
        self.assertTrue(details[1].startswith("done:steps=4:recovered=0:released=1:incarnation=dddddddddddd"))
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
        self.assertTrue(detail.startswith("failed:steps=1:recovered=0:cause=ingest:released=0"))
        # A Browser that could not be proven removed never ends its task as done, whatever it found.
        unreleased = self.make(FakeRuntime(release="browser-lost"))
        result, _terminal = self.run_with(unreleased, task.Outcome("done", summary="x", steps=1))
        self.assertEqual(result, {"status": "failed", "reason": "browser-lost", "files": []})
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
        self.assertLessEqual(self.brain.browser_step.call_args.kwargs["deadline"], task.MAX_SECONDS)
        self.assertEqual((payload["objective"], payload["language"]), ("Veja o prazo do edital.", "pt-BR"))
        self.brain.browser_step.side_effect = BrainRuntimeError("down")
        with mock.patch.object(service.cua, "CuaSession", return_value=session):
            result, _ = browser_service.run(self.turn, CONTEXT, REQUEST)
        self.assertEqual((result["status"], result["reason"]), ("failed", "model-unavailable"))

    def test_a_failed_driver_session_is_replaced_only_once_its_exec_is_proven_ended(self):
        class Stream:
            def __init__(self, exec_id, events):
                self.exec_id, self.events = exec_id, events

            def shutdown(self):
                self.events.append(("shutdown", self.exec_id))

            def close(self):
                self.events.append(("close", self.exec_id))

        stopping = []

        class Driving(FakeRuntime):
            ended = True
            stop_while_ending = ()

            open_failure = None

            def open_stream(self, lease, deadline, wanted):
                if self.open_failure is not None and any(event[0] == "stream" for event in self.events):
                    raise self.open_failure
                exec_id = f"exec-{sum(event[0] == 'stream' for event in self.events)}"
                self.events.append(("stream", exec_id))
                return exec_id, Stream(exec_id, self.events)

            def exited(self, exec_id, deadline):
                self.events.append(("exited", exec_id))
                stopping.extend(self.stop_while_ending)
                return self.ended

        page = cua.Observation("u", "t", "url: u", {})
        self.brain.browser_step.return_value = ({"kind": "done", "summary": "ok"}, 120)
        for ended, status in ((True, "done"), (False, "failed")):
            with self.subTest(ended=ended):
                fake = Driving()
                fake.ended = ended
                failed, fresh = mock.Mock(), mock.Mock()
                failed.observe.side_effect = cua.CuaError("channel-lost")
                fresh.observe.return_value = page
                with mock.patch.object(service.cua, "CuaSession", side_effect=[failed, fresh]):
                    result, _ = self.make(fake).run(self.turn, CONTEXT, REQUEST)
                self.assertEqual(result["status"], status)
                driven = [event for event in fake.events if event[0] in {"stream", "shutdown", "exited", "close"}]
                ending = [("stream", "exec-0"), ("shutdown", "exec-0"), ("exited", "exec-0"), ("close", "exec-0")]
                self.assertEqual(driven, [*ending, ("stream", "exec-1")] if ended else ending)
                detail = [fields["detail"] for operation, fields in self.audit if operation == "browser-task"][-1]
                expected = (
                    "done:steps=2:recovered=1:released=1"
                    if ended
                    else "failed:steps=0:recovered=0:cause=observe/channel-lost/reopen/driver-not-ended:released=1"
                )
                self.assertTrue(detail.startswith(expected), detail)

        # The replacement could not be opened: the task is lost, and says why.
        fake = Driving()
        fake.open_failure = runtime.BrowserError("browser-lost")
        failed = mock.Mock()
        failed.observe.side_effect = cua.CuaError("channel-lost")
        with mock.patch.object(service.cua, "CuaSession", side_effect=[failed]):
            result, _ = self.make(fake).run(self.turn, CONTEXT, REQUEST)
        self.assertEqual(result["status"], "failed")
        detail = [fields["detail"] for operation, fields in self.audit if operation == "browser-task"][-1]
        self.assertIn(":cause=observe/channel-lost/reopen/driver-unavailable:", detail)
        # Stop came while the failed session was ending: no replacement is opened, and the task was stopped.
        fake = Driving()
        fake.stop_while_ending = (True,)
        failed = mock.Mock()
        failed.observe.side_effect = cua.CuaError("channel-lost")
        stopped_turn = service.BrowserTurn("team_1", "d" * 64, TURN, "pt-BR", self.brain, lambda: bool(stopping))
        with mock.patch.object(service.cua, "CuaSession", side_effect=[failed]):
            result, _ = self.make(fake).run(stopped_turn, CONTEXT, REQUEST)
        self.assertEqual(result["status"], "stopped")
        self.assertEqual([event for event in fake.events if event[0] == "stream"], [("stream", "exec-0")])

    def test_no_driver_command_is_sent_once_stop_came_even_mid_initialization(self):
        stopping = []

        class Stream:
            """The Driver's stdio: answers each request, and Stop comes while `initialize` is answered."""

            def __init__(self):
                self.sent, self.pending = [], []

            def send(self, data, deadline):
                message = json.loads(data)
                self.sent.append(message.get("method"))
                if "id" in message:
                    self.pending.append(message["id"])

            def receive_line(self, deadline, limit):
                stopping.append(True)
                return json.dumps({"jsonrpc": "2.0", "id": self.pending.pop(0), "result": {}}).encode()

            def close(self):
                pass

        stream = Stream()

        class Driving(FakeRuntime):
            def open_stream(self, lease, deadline, wanted):
                return "exec-0", stream

        turn = service.BrowserTurn("team_1", "d" * 64, TURN, "pt-BR", self.brain, lambda: bool(stopping))
        with mock.patch.object(service.cua, "CuaSession", DRIVER_SESSION):
            result, _ = self.make(Driving()).run(turn, CONTEXT, REQUEST)
        self.assertEqual(result["status"], "stopped")
        self.assertEqual(stream.sent, ["initialize"])

    def test_a_driver_session_that_cannot_open_or_opens_after_stop_drives_nothing(self):
        stopping, closed = [], []

        class Stream:
            def close(self):
                closed.append(True)

        class Opening(FakeRuntime):
            failures = ()
            stop_while_opening = ()

            def open_stream(self, lease, deadline, wanted):
                self.events.append(("stream", lease.container_id))
                if self.failures:
                    raise self.failures.pop(0)
                stopping.extend(self.stop_while_opening)
                return "exec-0", Stream()

        # Docker could not open the first session: the task is lost where the runtime says.
        fake = Opening()
        fake.failures = [runtime.BrowserError("browser-lost")]
        result, _ = self.make(fake).run(self.turn, CONTEXT, REQUEST)
        self.assertEqual((result["status"], result["reason"]), ("failed", "browser-lost"))
        detail = [fields["detail"] for operation, fields in self.audit if operation == "browser-task"][-1]
        self.assertTrue(detail.startswith("failed:steps=0:recovered=0:cause=runtime/browser-lost:"), detail)
        # Stop came while Docker opened it: the session is closed unused and the task stopped.
        fake = Opening()
        fake.stop_while_opening = (True,)
        turn = service.BrowserTurn("team_1", "d" * 64, TURN, "pt-BR", self.brain, lambda: bool(stopping))
        with mock.patch.object(service.cua, "CuaSession", side_effect=AssertionError("no session")):
            result, _ = self.make(fake).run(turn, CONTEXT, REQUEST)
        self.assertEqual((result["status"], closed), ("stopped", [True]))

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

    def test_the_lease_is_released_from_the_moment_it_is_held_even_if_its_audit_fails(self):
        browser_service = self.make(FakeRuntime())
        with (
            mock.patch.object(local_audit, "record_request", side_effect=OSError("audit closed")),
            self.assertRaises(OSError),
        ):
            browser_service.run(self.turn, CONTEXT, REQUEST)
        self.assertEqual(self.runtime.events[-1], ("release", LEASE.container_id))

    def test_stop_removes_the_running_browser_and_nothing_is_ingested_after_it(self):
        listing = {"files": [{"name": "a.pdf", "size": 9}], "refused": [], "skipped": 0}
        browser_service = self.make(FakeRuntime(listing=listing, contents={"a.pdf": b"%PDF-1.4\n"}))
        stopped = [False]

        def run(_task):
            # Stop arrives while the task runs: the running Browser is removed at once.
            stopped[0] = True
            # Another turn's Stop, and another Team's, reach nothing.
            before = list(self.runtime.events)
            browser_service.interrupt("team_1", "1" * 32)
            browser_service.interrupt("team_2", TURN)
            self.assertEqual(self.runtime.events, before)
            browser_service.interrupt("team_1", TURN)
            return task.Outcome("done", summary="x", steps=1)

        turn = service.BrowserTurn("team_1", "d" * 64, TURN, "pt-BR", self.brain, lambda: stopped[0])
        with mock.patch.object(service.browser_task.Task, "run", autospec=True, side_effect=run):
            _result, terminal = browser_service.run(turn, CONTEXT, REQUEST)
        self.assertEqual(terminal, {"files": [], "lost": [], "skipped": 0})
        self.assertEqual(self.storage.stored, [])
        self.assertIn(("kill", LEASE.container_id), self.runtime.events)
        # Once the task ended, Stop finds no Browser of the Team; a removal that failed leaves the release to prove it.
        browser_service.interrupt("team_1", TURN)
        self.assertEqual(self.runtime.events.count(("kill", LEASE.container_id)), 1)
        self.runtime.kill_error = True
        with mock.patch.object(service.browser_task.Task, "run", autospec=True, side_effect=run):
            browser_service.run(turn, CONTEXT, REQUEST)
        self.assertEqual(self.runtime.events[-1], ("release", LEASE.container_id))

    def test_a_stop_during_creation_opens_no_channel(self):
        stopped = [False]
        fake = FakeRuntime()
        original = fake.acquire

        def acquire(team_id, language):
            stopped[0] = True
            return original(team_id, language)

        fake.acquire = acquire
        browser_service = self.make(fake)
        turn = service.BrowserTurn("team_1", "d" * 64, TURN, "pt-BR", self.brain, lambda: stopped[0])
        result, _terminal = browser_service.run(turn, CONTEXT, REQUEST)
        self.assertEqual(result["status"], "stopped")
        self.assertNotIn(("stream", LEASE.container_id), self.runtime.events)
        self.assertEqual(self.runtime.events[-1], ("release", LEASE.container_id))

    def test_a_stop_while_a_download_is_read_stores_it_not(self):
        listing = {"files": [{"name": "a.pdf", "size": 9}], "refused": [], "skipped": 0}
        fake = FakeRuntime(listing=listing, contents={"a.pdf": b"%PDF-1.4\n"})
        browser_service = self.make(fake)
        original = fake.read

        def read(lease, name, size):
            # Stop of this very turn arrives while the bytes are read: the store refuses them.
            browser_service.interrupt("team_1", TURN)
            return original(lease, name, size)

        fake.read = read
        turn = service.BrowserTurn("team_1", "d" * 64, TURN, "pt-BR", self.brain, lambda: False)
        _result, terminal = self.run_with_turn(browser_service, turn, task.Outcome("done", summary="x", steps=1))
        self.assertEqual((terminal["files"], self.storage.stored), ([], []))

    def test_an_egress_replaced_during_the_ingest_loses_the_task(self):
        listing = {"files": [{"name": "a.pdf", "size": 9}], "refused": [], "skipped": 0}
        fake = FakeRuntime(listing=listing, contents={"a.pdf": b"%PDF-1.4\n"})
        fake.intact_after = False
        result, terminal = self.run_with(self.make(fake), task.Outcome("done", summary="x", steps=1))
        self.assertEqual((result["status"], result["reason"]), ("failed", "browser-lost"))
        # What it stored is still named, so the reply says so; each stays from the web.
        self.assertEqual([item["name"] for item in terminal["files"]], ["a.pdf"])

    def test_storage_failures_become_closed_losses(self):
        listing = {"files": [{"name": "a.pdf", "size": 9}], "refused": [], "skipped": 0}
        browser_service = self.make(FakeRuntime(listing=listing, contents={"a.pdf": b"%PDF-1.4\n"}))
        self.storage.put = mock.Mock(side_effect=team_storage.StorageQuotaError("full"))
        _, terminal = self.run_with(browser_service, task.Outcome("done", summary="x", steps=1))
        self.assertEqual(terminal["lost"], [{"name": "a.pdf", "reason": "quota-exceeded"}])

    def test_a_screen_view_belongs_to_its_task_alone(self):
        browser_service = self.make(FakeRuntime())
        with self.assertRaises(runtime.BrowserError) as idle:
            browser_service.open_view("team_1")
        self.assertEqual(idle.exception.code, "browser-not-running")
        seen = {}

        def run(_task):
            view_id = browser_service.open_view("team_1")
            seen["view"] = view_id
            self.assertRegex(view_id, r"^[0-9a-f]{32}$")
            with self.assertRaises(runtime.BrowserError):
                browser_service.open_view("team_2")
            for team_id, other in (("team_1", "0" * 32), ("team_2", view_id)):
                with self.assertRaises(runtime.BrowserError) as refused:
                    browser_service.carry_view(team_id, other, "client", "key", lambda _first: None)
                self.assertEqual(refused.exception.code, "browser-not-running")
            with mock.patch.object(local_view.View, "carry") as carried:
                browser_service.carry_view("team_1", view_id, "client", "key", "upgraded")
                carried.assert_called_once_with("client", "key", "upgraded")
            return task.Outcome("done", summary="x", steps=1)

        with mock.patch.object(service.browser_task.Task, "run", autospec=True, side_effect=run):
            browser_service.run(self.turn, CONTEXT, REQUEST)
        self.assertIn("view", seen)
        # The task ended: neither its view nor a new one opens.
        for call in (
            lambda: browser_service.open_view("team_1"),
            lambda: browser_service.carry_view("team_1", seen["view"], "client", "key", lambda _first: None),
        ):
            with self.assertRaises(runtime.BrowserError):
                call()

    def test_a_released_task_ends_its_view_and_its_relay_reaches_the_runtime(self):
        made = []

        class RecordingView:
            def __init__(self, open_program, exited, record):
                self.id = "a" * 32
                self.calls = (open_program, exited, record)
                self.ended = False
                made.append(self)

            def end(self):
                self.ended = True

        fake = FakeRuntime()
        fake.open_relay = mock.Mock(return_value=("exec-1", "stream"))
        fake.exited = mock.Mock(return_value=True)
        browser_service = self.make(fake)
        with mock.patch.object(service.local_view, "View", RecordingView):
            self.run_with(browser_service, task.Outcome("done", summary="x", steps=1))
        [recorded] = made
        self.assertTrue(recorded.ended)
        open_program, exited, record = recorded.calls
        self.assertEqual(open_program(), ("exec-1", "stream"))
        fake.open_relay.assert_called_once_with(LEASE)
        self.assertTrue(exited("exec-1", 5.0))
        fake.exited.assert_called_once_with("exec-1", 5.0)
        record("started")
        self.assertIn(
            (
                "browser-view",
                {"result": "ok", "team_id": "team_1", "detail": f"started:container={LEASE.container_id[:12]}"},
            ),
            self.audit,
        )

    def test_streams_hold_one_of_two_slots(self):
        browser_service = self.make(FakeRuntime())
        with browser_service.view_slot(), browser_service.view_slot():
            with self.assertRaises(runtime.BrowserError) as busy, browser_service.view_slot():
                pass
            self.assertEqual(busy.exception.code, "browser-view-busy")
        with browser_service.view_slot():
            pass

    def test_a_segments_runner_binds_the_team_incarnation_language_brain_and_stop_at_run_time(self):
        seen = []
        controller = mock.Mock()
        controller.assistant_lifecycle._network.return_value.id = "d" * 64
        controller._chat_cancelled.side_effect = lambda turn_token: turn_token == STOPPED_TURN
        controller.browser_service.run.side_effect = lambda turn, context, request: seen.append(turn) or ("r", "t")
        request = mock.Mock(team_id="team_1", locale="pt-BR", token=STOPPED_TURN, routine=None, recording=None)
        self.assertEqual(local_chat_segment._browser_runner(controller, request)(CONTEXT, REQUEST), ("r", "t"))
        [turn] = seen
        self.assertEqual(
            (turn.team_id, turn.incarnation, turn.token, turn.locale), ("team_1", "d" * 64, STOPPED_TURN, "pt-BR")
        )
        self.assertIs(turn.runtime, controller.brain_runtime)
        self.assertTrue(turn.cancelled())


if __name__ == "__main__":
    unittest.main()
