"""Chat attachment rules shared by both profiles (ADR-0093)."""

import dataclasses
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

from assistant.spec import ActionSpec
from chat import attachments as chat_attachments
from chat import orchestrator as chat_orchestrator
from inference import client as brain_runtime_client
from storage import files as team_storage
from tests.test_chat_orchestrator import FakeRuntime, accept_input, completed, context, strategy, suspended

TEXT = {"id": "a" * 32, "content": {"type": "text", "text": "notes", "pdf": False}}
IMAGE = {"id": "b" * 32, "content": {"type": "image", "media_type": "image/png"}}
OPAQUE = {"id": "c" * 32, "content": {"type": "opaque", "reason": "unsupported"}}


def _action(*human_requests: str, input_files: tuple[str, ...] = ()) -> ActionSpec:
    return ActionSpec(
        "Act", {"type": "object"}, {"type": "object"}, human_requests=human_requests, input_files=input_files
    )


class AttachmentRuleTests(unittest.TestCase):
    def test_the_brain_learns_which_actions_authorize_and_which_take_a_file(self) -> None:
        plain = chat_attachments.runtime_action("read", _action("input:text"))
        approved = chat_attachments.runtime_action("upload", _action("approval", input_files=("document",)))
        passkey = chat_attachments.runtime_action("delete", _action("auth:passkey"))
        self.assertEqual((plain.authorization, plain.input_files), (False, ()))
        self.assertEqual((approved.authorization, approved.input_files), (True, ("document",)))
        self.assertTrue(passkey.authorization)
        digest = brain_runtime_client.contract_digest(brain_runtime_client.RuntimeAssistant("a", "Genesis.", (plain,)))
        changed = brain_runtime_client.contract_digest(
            brain_runtime_client.RuntimeAssistant("a", "Genesis.", (dataclasses.replace(plain, authorization=True),))
        )
        self.assertNotEqual(digest, changed)

    def test_only_text_or_image_content_restricts_the_turn(self) -> None:
        self.assertTrue(chat_attachments.reads_content([OPAQUE, TEXT]))
        self.assertTrue(chat_attachments.reads_content([IMAGE]))
        self.assertFalse(chat_attachments.reads_content([OPAQUE]))
        self.assertFalse(chat_attachments.reads_content([]))

    def test_stored_files_read_lazily_and_refuse_changed_bytes(self) -> None:
        reads: list[str] = []

        def read(file_id: str) -> tuple[dict[str, object], bytes]:
            reads.append(file_id)
            return ({"sha256": "f" * 64, "size": 3}, b"abc")

        files = chat_attachments.stored_files([{"id": "a" * 32, "name": "a.txt", "size": 3, "sha256": "f" * 64}], read)
        self.assertEqual(reads, [])
        self.assertEqual(files[0].read(), b"abc")
        changed = chat_attachments.stored_files(
            [{"id": "a" * 32, "name": "a.txt", "size": 3, "sha256": "e" * 64}], read
        )
        with self.assertRaises(chat_attachments.AttachmentIntegrityError):
            changed[0].read()


def _gated_context(*attachments: dict[str, object]) -> brain_runtime_client.RuntimeContext:
    actions = (
        brain_runtime_client.RuntimeAction("hello", "Greet.", {"type": "object"}),
        brain_runtime_client.RuntimeAction("publish", "Publish.", {"type": "object"}, authorization=True),
    )
    return dataclasses.replace(context(*actions), attachments=attachments)


class AuthorizationGateTests(unittest.TestCase):
    def test_attachment_content_admits_only_authorizing_actions(self) -> None:
        invoked: list[str] = []
        with self.assertRaises(chat_orchestrator.ChatOrchestrationError):
            chat_orchestrator.run(
                FakeRuntime([suspended("hello")]),
                _gated_context(TEXT),
                "Use the notes",
                strategy(accept_input, lambda request: invoked.append(request.action)),
            )
        self.assertEqual(invoked, [])
        outcome = chat_orchestrator.run(
            FakeRuntime([suspended("publish"), completed()]),
            _gated_context(TEXT),
            "Publish the notes",
            strategy(accept_input, lambda request: invoked.append(request.action) or {"ok": True}),
        )
        self.assertEqual(invoked, ["publish"])
        self.assertEqual(outcome.reply, "Done")

    def test_opaque_attachments_leave_every_action_available(self) -> None:
        invoked: list[str] = []
        chat_orchestrator.run(
            FakeRuntime([suspended("hello"), completed()]),
            _gated_context(OPAQUE),
            "Greet",
            strategy(accept_input, lambda request: invoked.append(request.action) or {"ok": True}),
        )
        self.assertEqual(invoked, ["hello"])


class _Paused:
    """A challenge store holding one paused turn per Team."""

    def __init__(self, **turns: object) -> None:
        self.turns = turns
        self.asked: list[str] = []

    def current(self, team_id: str) -> object:
        self.asked.append(team_id)
        return self.turns.get(team_id)


class RetentionRuleTests(unittest.TestCase):
    def test_the_first_paused_turn_names_the_files_and_an_unreadable_one_names_every_file(self) -> None:
        human = _Paused(team_1=types.SimpleNamespace(payload=types.SimpleNamespace(file_ids=("a",))))
        integration = _Paused(team_1=types.SimpleNamespace(payload=types.SimpleNamespace(file_ids=("b",))))
        self.assertEqual(chat_attachments.paused_files("team_1", (human, integration)), ("a",))
        self.assertEqual(integration.asked, [])
        self.assertEqual(chat_attachments.paused_files("team_1", (integration, human)), ("b",))
        unreadable = _Paused(team_1=types.SimpleNamespace(payload=object()))
        self.assertEqual(chat_attachments.paused_files("team_1", (_Paused(), unreadable)), ("*",))
        # Another Team's paused turn is never this Team's, and no paused turn differs from one that selected nothing.
        self.assertIsNone(chat_attachments.paused_files("team_2", (human, integration)))
        empty = _Paused(team_1=types.SimpleNamespace(payload=types.SimpleNamespace(file_ids=())))
        self.assertEqual(chat_attachments.paused_files("team_1", (empty,)), ())

    def test_a_file_is_forgotten_only_when_the_thread_or_a_paused_turn_may_still_deliver_it(self) -> None:
        storage = mock.Mock(referenced=mock.Mock(return_value=frozenset({"a"})))
        self.assertTrue(chat_attachments.forget_required(storage, "team_1", "a", None))
        storage.referenced.assert_called_once_with("team_1")
        for pending, expected in ((None, False), ((), False), (("c",), False), (("b",), True), (("*",), True)):
            with self.subTest(pending=pending):
                self.assertIs(chat_attachments.forget_required(storage, "team_1", "b", pending), expected)

    def test_turn_references_settle_and_release_without_failing_the_turn(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            storage = team_storage.TeamStorage(Path(directory) / "teams")
            first = storage.put("team_1", "a.txt", b"a", "text/plain")["id"]
            second = storage.put("team_1", "b.txt", b"b", "text/plain")["id"]
            storage.reference("team_1", [first, second])
            chat_attachments.release_failed_turn(lambda: storage, "team_1", (second,))
            self.assertEqual(storage.referenced("team_1"), frozenset({first}))
            chat_attachments.settle_completed_turn(lambda: storage, "team_1", [second])
            self.assertEqual(storage.referenced("team_1"), frozenset({second}))
        unavailable = mock.Mock(
            release=mock.Mock(side_effect=team_storage.StorageError("unavailable")),
            settle=mock.Mock(side_effect=team_storage.StorageError("unavailable")),
        )
        opened = mock.Mock(return_value=unavailable)
        # Nothing newly referenced opens no storage at all.
        chat_attachments.release_failed_turn(opened, "team_1", ())
        opened.assert_not_called()
        chat_attachments.release_failed_turn(opened, "team_1", ("a",))
        chat_attachments.settle_completed_turn(opened, "team_1", ())
        unavailable.settle.assert_called_once_with("team_1", ())
        # Storage that cannot even be opened fails like a release that fails: the turn stands, references stay.
        closed = mock.Mock(side_effect=team_storage.StorageError("storage root is unavailable"))
        chat_attachments.release_failed_turn(closed, "team_1", ("a",))
        chat_attachments.settle_completed_turn(closed, "team_1", ())
        self.assertEqual(closed.call_count, 2)


if __name__ == "__main__":
    unittest.main()
