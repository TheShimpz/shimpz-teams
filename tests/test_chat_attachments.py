"""Chat attachment rules shared by both profiles (ADR-0093)."""

from __future__ import annotations

import dataclasses
import unittest

from assistant.spec import ActionSpec
from chat import attachments as chat_attachments
from chat import orchestrator as chat_orchestrator
from inference import client as brain_runtime_client
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


if __name__ == "__main__":
    unittest.main()
