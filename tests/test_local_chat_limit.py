"""The Space's chat execution limit (ADR-0119): Team's own record, and the admission that counts against it."""

import json
import tempfile
import threading
import unittest
from http import HTTPStatus
from pathlib import Path
from unittest import mock

from core.http import strict as strict_http
from local.chat import limit as local_chat_limit
from local.chat.service import ChatTurnService
from local.composition import ChatTurnDependencies
from local.errors import ApiProblemError
from local.http import chat as http_chat


class ChatLimitStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name) / "inference"

    def test_the_limit_is_the_default_until_a_save_commits_one_that_a_restart_reads_back(self) -> None:
        store = local_chat_limit.ChatLimitStore(self.root)
        self.assertEqual(store.current(), local_chat_limit.DEFAULT)
        self.assertEqual(store.save(1), 1)
        self.assertEqual(store.current(), 1)
        path = self.root / local_chat_limit.NAME
        self.assertEqual(json.loads(path.read_text()), {"schema": 1, "limit": 1})
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        self.assertEqual(local_chat_limit.ChatLimitStore(self.root).current(), 1)
        store.delete()
        self.assertEqual(store.current(), local_chat_limit.DEFAULT)
        store.delete()

    def test_a_record_that_is_not_the_stores_own_fails_closed(self) -> None:
        self.root.mkdir()
        path = self.root / local_chat_limit.NAME
        for record in (
            {"schema": 1, "limit": 3},
            {"schema": 2, "limit": 1},
            {"schema": True, "limit": 1},
            {"schema": 1, "limit": 1, "extra": 1},
            [1],
            "not json",
        ):
            with self.subTest(record=record):
                path.write_text(record if isinstance(record, str) else json.dumps(record))
                with self.assertRaises(ApiProblemError) as refused:
                    local_chat_limit.ChatLimitStore(self.root).current()
                self.assertEqual(refused.exception.code, "chat-limit-unavailable")

    def test_a_save_or_delete_that_cannot_reach_the_volume_fails_closed(self) -> None:
        store = local_chat_limit.ChatLimitStore(self.root)
        failing = mock.patch.object(local_chat_limit.private_state, "write_json_record", side_effect=OSError("full"))
        with failing, self.assertRaises(ApiProblemError):
            store.save(1)
        self.assertEqual(store.current(), local_chat_limit.DEFAULT)
        with mock.patch.object(Path, "unlink", side_effect=OSError("read-only")), self.assertRaises(ApiProblemError):
            store.delete()


class ChatLimitAdmissionTests(unittest.TestCase):
    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.limit = local_chat_limit.ChatLimitStore(Path(directory.name))
        self.service = ChatTurnService(ChatTurnDependencies(chat_limit=self.limit))

    def test_a_turn_beyond_the_limit_is_refused_and_a_routine_segment_never_counts(self) -> None:
        self.limit.save(1)
        with self.service._exclusive_chat_turn("team_1"):
            with self.assertRaises(ApiProblemError) as refused, self.service._exclusive_chat_turn("team_2"):
                raise AssertionError("a second turn ran beside the limit")
            self.assertEqual((refused.exception.status, refused.exception.code), (429, "space-busy"))
            # The refused turn released its Team's slot: that Team is free for its next attempt.
            freed = self.service._chat_lock("team_2")
            self.assertTrue(freed.acquire(blocking=False))
            freed.release()
        routine = self.service._exclusive_chat_turn("team_1", routine_id="r" * 32)
        with routine, self.service._exclusive_chat_turn("team_2"):
            self.assertEqual(self.service._chat_executions(), 1)

    def test_a_lowered_limit_lets_running_turns_drain_and_admits_none_beside_them(self) -> None:
        with self.service._exclusive_chat_turn("team_1"), self.service._exclusive_chat_turn("team_2"):
            self.limit.save(1)
            self.assertEqual(self.service._chat_executions(), 2)
            with self.assertRaises(ApiProblemError), self.service._exclusive_chat_turn("team_3"):
                raise AssertionError("a turn started over a lowered limit")
        with self.service._exclusive_chat_turn("team_3"):
            self.assertEqual(self.service._chat_executions(), 1)

    def test_concurrent_admissions_never_exceed_the_limit(self) -> None:
        # Every request reaches registration at once, as a request a crashed Admin left behind would: the count and the
        # registration are one step, so exactly the limit's worth start.
        teams = [f"team_{index}" for index in range(8)]
        barrier = threading.Barrier(len(teams))
        release = threading.Event()
        admitted, refused = [], []

        def enter(team_id: str) -> None:
            barrier.wait()
            try:
                with self.service._exclusive_chat_turn(team_id):
                    admitted.append(team_id)
                    release.wait(5)
            except ApiProblemError as exc:
                refused.append(exc.code)

        threads = [threading.Thread(target=enter, args=(team_id,)) for team_id in teams]
        for thread in threads:
            thread.start()
        deadline = threading.Event()
        for _ in range(500):
            if len(admitted) + len(refused) == len(teams):
                break
            deadline.wait(0.01)
        self.assertEqual((len(admitted), refused), (local_chat_limit.DEFAULT, ["space-busy"] * 6))
        release.set()
        for thread in threads:
            thread.join(5)

    def test_a_save_is_never_interleaved_with_an_admission(self) -> None:
        # The save waits for the guard an admission holds while it counts and registers, and the reverse.
        self.service._active_chat_guard.acquire()
        saved = threading.Event()
        saver = threading.Thread(target=lambda: (self.service.save_chat_limit(1), saved.set()))
        saver.start()
        self.assertFalse(saved.wait(0.2))
        self.service._active_chat_guard.release()
        self.assertTrue(saved.wait(5))
        saver.join(5)
        self.assertEqual(self.limit.current(), 1)

    def test_an_unreadable_limit_refuses_the_turn_and_frees_its_team(self) -> None:
        unreadable = mock.patch.object(self.limit, "current", side_effect=local_chat_limit._unavailable())
        with unreadable, self.assertRaises(ApiProblemError) as refused, self.service._exclusive_chat_turn("team_1"):
            raise AssertionError("a turn ran without a readable limit")
        self.assertEqual(refused.exception.code, "chat-limit-unavailable")
        self.assertEqual(self.service._active_chat_tokens, {})
        with self.service._exclusive_chat_turn("team_1"):
            pass


class ChatLimitRouteTests(unittest.TestCase):
    def test_the_supervisor_reads_and_saves_the_limit_in_its_exact_shape(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = local_chat_limit.ChatLimitStore(Path(directory))
            handler = mock.Mock()
            handler.server.controller.chat_limit = store
            handler.server.controller.chat_turn_service = ChatTurnService(ChatTurnDependencies(chat_limit=store))
            read = strict_http.resolve_controller_route("GET", ("v1", "chat", "limit"))
            save = strict_http.resolve_controller_route("PUT", ("v1", "chat", "limit"))
            self.assertEqual(
                http_chat.OPERATIONS[read.operation](handler, read),
                (HTTPStatus.OK, {"limit": 2}, "chat-limit-read", None, None),
            )
            handler.body.return_value = {"limit": 1}
            self.assertEqual(
                http_chat.OPERATIONS[save.operation](handler, save),
                (HTTPStatus.OK, {"limit": 1}, "chat-limit-save", None, None),
            )
            for body in ({"limit": 3}, {"limit": "1"}, {}):
                with self.subTest(body=body):
                    handler.body.return_value = body
                    with self.assertRaises(ApiProblemError) as refused:
                        http_chat.OPERATIONS[save.operation](handler, save)
                    self.assertEqual(refused.exception.code, "invalid-body")
            self.assertEqual(store.current(), 1)


if __name__ == "__main__":
    unittest.main()
