"""Concurrency contracts for Local Assistant snapshot cutovers."""

from __future__ import annotations

import threading
import unittest
from types import SimpleNamespace
from unittest import mock

from local.assistant import lifecycle as assistant_lifecycle
from local.errors import ApiProblemError


class LocalSnapshotCutoverTests(unittest.TestCase):
    def test_published_to_local_cutover_holds_one_chat_slot(self) -> None:
        lock = threading.Lock()
        previous = SimpleNamespace(
            provenance="published",
            assistant_id="fixture-assistant",
            binding_digest="sha256:" + ("1" * 64),
        )
        events: list[str] = []
        lifecycle = SimpleNamespace(
            registry=SimpleNamespace(binding=lambda _team_id, _assistant_id: previous),
            chat_turn_service=SimpleNamespace(_chat_lock=lambda _team_id: lock),
        )

        def uninstall(_team_id: str, _assistant_id: str) -> None:
            events.append("uninstall")
            self.assertFalse(lock.acquire(blocking=False))

        def install(_team_id: str, _assistant_id: str) -> dict[str, object]:
            events.append("install")
            self.assertTrue(lock.locked())
            return {"assistant": "fixture-assistant", "installed": True}

        lifecycle._uninstall_assistant_unguarded = uninstall
        lifecycle._install_assistant_unguarded = install

        result = assistant_lifecycle.replace_published_with_local(
            lifecycle,
            "team_1",
            previous,
            lambda install_successor: install_successor("team_1", "fixture-assistant"),
        )

        self.assertEqual(events, ["uninstall", "install"])
        self.assertEqual(result, {"assistant": "fixture-assistant", "installed": True})
        self.assertFalse(lock.locked())

    def test_published_to_local_cutover_rejects_a_changed_binding_before_uninstall(self) -> None:
        previous = SimpleNamespace(
            provenance="published",
            assistant_id="fixture-assistant",
            binding_digest="sha256:" + ("1" * 64),
        )
        lifecycle = SimpleNamespace(
            registry=SimpleNamespace(
                binding=lambda _team_id, _assistant_id: SimpleNamespace(
                    provenance="published",
                    assistant_id="fixture-assistant",
                    binding_digest="sha256:" + ("2" * 64),
                )
            ),
            chat_turn_service=SimpleNamespace(_chat_lock=lambda _team_id: threading.Lock()),
            _uninstall_assistant_unguarded=mock.Mock(),
            _install_assistant_unguarded=mock.Mock(),
        )

        with self.assertRaises(ApiProblemError) as caught:
            assistant_lifecycle.replace_published_with_local(
                lifecycle,
                "team_1",
                previous,
                mock.Mock(),
            )

        self.assertEqual(caught.exception.code, "assistant-binding-conflict")
        lifecycle._uninstall_assistant_unguarded.assert_not_called()

    def test_published_to_local_cutover_rejects_an_absent_binding_before_uninstall(self) -> None:
        previous = SimpleNamespace(
            provenance="published",
            assistant_id="fixture-assistant",
        )
        lifecycle = SimpleNamespace(
            registry=SimpleNamespace(binding=lambda _team_id, _assistant_id: None),
            chat_turn_service=SimpleNamespace(_chat_lock=lambda _team_id: threading.Lock()),
            _uninstall_assistant_unguarded=mock.Mock(),
            _install_assistant_unguarded=mock.Mock(),
        )

        with self.assertRaises(ApiProblemError) as caught:
            assistant_lifecycle.replace_published_with_local(
                lifecycle,
                "team_1",
                previous,
                mock.Mock(),
            )

        self.assertEqual(caught.exception.code, "assistant-binding-conflict")
        lifecycle._uninstall_assistant_unguarded.assert_not_called()

    def test_fresh_local_install_rechecks_absence_inside_one_chat_slot(self) -> None:
        lock = threading.Lock()

        def binding(_team_id: str, _assistant_id: str) -> None:
            self.assertTrue(lock.locked())

        def install(_team_id: str, _assistant_id: str) -> dict[str, object]:
            self.assertTrue(lock.locked())
            return {"assistant": "fixture-assistant", "installed": True}

        lifecycle = SimpleNamespace(
            registry=SimpleNamespace(binding=binding),
            chat_turn_service=SimpleNamespace(_chat_lock=lambda _team_id: lock),
            _install_assistant_unguarded=install,
        )

        result = assistant_lifecycle.install_fresh_local(
            lifecycle,
            "team_1",
            "fixture-assistant",
            lambda install_successor: install_successor("team_1", "fixture-assistant"),
        )

        self.assertEqual(result, {"assistant": "fixture-assistant", "installed": True})
        self.assertFalse(lock.locked())

    def test_fresh_local_install_rejects_a_concurrent_binding_before_write(self) -> None:
        install_successor = mock.Mock()
        lifecycle = SimpleNamespace(
            registry=SimpleNamespace(binding=lambda _team_id, _assistant_id: object()),
            chat_turn_service=SimpleNamespace(_chat_lock=lambda _team_id: threading.Lock()),
            _install_assistant_unguarded=mock.Mock(),
        )

        with self.assertRaises(ApiProblemError) as caught:
            assistant_lifecycle.install_fresh_local(
                lifecycle,
                "team_1",
                "fixture-assistant",
                install_successor,
            )

        self.assertEqual(caught.exception.code, "assistant-binding-conflict")
        install_successor.assert_not_called()


if __name__ == "__main__":
    unittest.main()
