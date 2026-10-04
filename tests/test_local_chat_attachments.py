"""Local hydration of a chat segment's selected files (ADR-0093)."""

from __future__ import annotations

import threading
import unittest
from types import SimpleNamespace
from unittest import mock

from chat import attachments as chat_attachments
from chat import orchestrator as chat_orchestrator
from local.chat import attachments as local_attachments
from local.errors import ApiProblemError
from prepare import helper as preparation_helper
from prepare import service as preparation
from storage import files as team_storage

FILES = [{"id": "a" * 32, "name": "notes.txt", "media_type": "text/plain", "size": 5, "sha256": "f" * 64}]


def _service(**changes: object) -> SimpleNamespace:
    service = SimpleNamespace(
        space_id="space",
        storage=SimpleNamespace(get=lambda _team_id, _file_id: ({"sha256": "f" * 64, "size": 5}, b"notes")),
        assistant_lifecycle=SimpleNamespace(client=object(), cpuset_cpus="0"),
        _active_chat_guard=threading.Lock(),
        _active_chat_tokens={"team_1": "token"},
        _cancelled_chat_tokens=set(),
        _active_action_containers={},
        _raise_storage_problem=mock.Mock(side_effect=ApiProblemError(503, "storage", code="storage-unavailable")),
    )
    service._chat_cancelled = lambda token: token in service._cancelled_chat_tokens
    for key, value in changes.items():
        setattr(service, key, value)
    return service


class LocalTurnAttachmentTests(unittest.TestCase):
    def test_no_selected_file_prepares_nothing(self) -> None:
        with mock.patch.object(local_attachments.local_prepare, "prepare_attachments") as prepare:
            self.assertEqual(local_attachments.turn_attachments(_service(), "team_1", "token", []), ())
        prepare.assert_not_called()

    def test_text_is_prepared_and_wired_for_the_brain(self) -> None:
        attachments = local_attachments.turn_attachments(_service(), "team_1", "token", FILES)
        self.assertEqual(attachments[0]["content"], {"type": "text", "text": "notes", "pdf": False})
        self.assertEqual(attachments[0]["sha256"], "f" * 64)

    def test_a_running_helper_occupies_the_stop_slot_until_it_is_removed(self) -> None:
        service = _service()
        helper = object()
        seen: list[object] = []

        def prepare(*_args: object, stop, **_kwargs: object) -> tuple[preparation.Attachment, ...]:
            stop.started(helper)
            seen.append(dict(service._active_action_containers))
            stop.stopped(helper)
            return ()

        with mock.patch.object(local_attachments.local_prepare, "prepare_attachments", side_effect=prepare):
            local_attachments.turn_attachments(service, "team_1", "token", FILES)
        self.assertEqual(seen, [{"team_1": ("token", helper)}])
        self.assertEqual(service._active_action_containers, {})

    def test_a_stopped_turn_never_starts_a_helper_or_continues(self) -> None:
        service = _service(_cancelled_chat_tokens={"token"})

        def prepare(*_args: object, stop, **_kwargs: object) -> tuple[preparation.Attachment, ...]:
            stop.started(object())
            return ()

        with (
            mock.patch.object(local_attachments.local_prepare, "prepare_attachments", side_effect=prepare),
            self.assertRaises(chat_orchestrator.ChatStoppedError),
        ):
            local_attachments.turn_attachments(service, "team_1", "token", FILES)
        with (
            mock.patch.object(local_attachments.local_prepare, "prepare_attachments", return_value=()),
            self.assertRaises(chat_orchestrator.ChatStoppedError),
        ):
            local_attachments.turn_attachments(service, "team_1", "token", FILES)

    def test_preparation_is_interrupted_once_its_turn_is_stopped(self) -> None:
        service = _service()
        checks: list[str] = []

        def prepare(*_args: object, stop, **_kwargs: object) -> tuple[preparation.Attachment, ...]:
            stop.interrupt()
            checks.append("running")
            service._cancelled_chat_tokens.add("token")
            stop.interrupt()
            checks.append("unreachable")
            return ()

        with (
            mock.patch.object(local_attachments.local_prepare, "prepare_attachments", side_effect=prepare),
            self.assertRaises(chat_orchestrator.ChatStoppedError),
        ):
            local_attachments.turn_attachments(service, "team_1", "token", FILES)
        self.assertEqual(checks, ["running"])

    def test_every_preparation_failure_has_an_explicit_public_outcome(self) -> None:
        cases = (
            (preparation.AttachmentLimitError("attachments-too-many-images"), 422, "attachments-too-many-images"),
            (preparation_helper.HelperUnavailableError("down"), 503, "attachments-unavailable"),
            (chat_attachments.AttachmentIntegrityError("changed"), 409, "team-context-changed"),
            (team_storage.StorageNotFoundError("gone"), 404, "file-not-found"),
            (team_storage.StorageError("broken"), 503, "storage-unavailable"),
        )
        for error, status, code in cases:
            with (
                self.subTest(code=code),
                mock.patch.object(local_attachments.local_prepare, "prepare_attachments", side_effect=error),
                self.assertRaises(ApiProblemError) as raised,
            ):
                local_attachments.turn_attachments(_service(), "team_1", "token", FILES)
            self.assertEqual((int(raised.exception.status), raised.exception.code), (status, code))

    def test_stopping_a_helper_that_no_longer_holds_the_slot_leaves_the_slot_alone(self) -> None:
        service = _service()
        holder = ("token", object())

        def prepare(*_args: object, stop, **_kwargs: object) -> tuple[preparation.Attachment, ...]:
            stop.stopped(object())
            service._active_action_containers["team_1"] = holder
            stop.stopped(object())
            return ()

        with mock.patch.object(local_attachments.local_prepare, "prepare_attachments", side_effect=prepare):
            local_attachments.turn_attachments(service, "team_1", "token", FILES)
        self.assertEqual(service._active_action_containers, {"team_1": holder})


if __name__ == "__main__":
    unittest.main()
