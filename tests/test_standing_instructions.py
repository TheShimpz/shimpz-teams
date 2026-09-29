"""Standing instructions: Supervisor-saved, private per Team, deleted with it, and sent to Brain (ADR-0083)."""

from __future__ import annotations

import dataclasses
import json
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from test_brain_runtime_client import RuntimeClientCase, _Response, context

from inference import config as inference_config
from local import app as local_app
from local import inference as local_inference

RULES = ["Responda sempre em português do Brasil.", "Use listas curtas, sem tabelas."]


class StoreTests(unittest.TestCase):
    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name) / "inference"
        self.store = inference_config.InferenceConfigStore(self.root)

    def test_rules_round_trip_privately_and_an_empty_list_removes_them(self):
        self.assertEqual(self.store.load_instructions("team_1"), [])
        self.assertEqual(self.store.save_instructions("team_1", RULES), RULES)
        self.assertEqual(self.store.load_instructions("team_1"), RULES)
        self.assertEqual(self.store.load_instructions("team_2"), [])
        [stored] = self.root.glob("*.instructions.json")
        self.assertEqual(stored.stat().st_mode & 0o777, 0o600)
        self.assertEqual(json.loads(stored.read_text(encoding="utf-8"))["instructions"], RULES)
        self.assertEqual(self.store.save_instructions("team_1", []), [])
        self.assertEqual(list(self.root.glob("*.instructions.json")), [])

    def test_invalid_rules_and_team_ids_are_refused(self):
        with self.assertRaises(inference_config.InferenceConfigError):
            self.store.save_instructions("team_1", ["Linha\nquebrada"])
        with self.assertRaises(inference_config.InferenceConfigError):
            self.store.save_instructions("../team", RULES)

    def test_a_corrupt_or_foreign_file_fails_closed(self):
        self.store.save_instructions("team_1", RULES)
        [stored] = self.root.glob("*.instructions.json")
        for raw in (
            b"not json",
            b"\xff",
            json.dumps({"schema": 1, "team_id": "team_2", "instructions": RULES}).encode(),
            json.dumps({"schema": 2, "team_id": "team_1", "instructions": RULES}).encode(),
            json.dumps({"schema": 1, "team_id": "team_1", "instructions": []}).encode(),
            json.dumps({"schema": 1, "team_id": "team_1"}).encode(),
            json.dumps(["not", "an", "object"]).encode(),
        ):
            stored.write_bytes(raw)
            with self.subTest(raw=raw), self.assertRaises(inference_config.InferenceConfigError):
                self.store.load_instructions("team_1")
        with (
            mock.patch.object(Path, "read_bytes", side_effect=PermissionError("denied")),
            self.assertRaises(inference_config.InferenceConfigError),
        ):
            self.store.load_instructions("team_1")

    def test_deleting_the_team_inference_state_removes_the_rules_too(self):
        self.store.save("team_1", inference_config.normalize("openai", "gpt-6-luna"))
        self.store.save_instructions("team_1", RULES)
        self.store.delete("team_1")
        self.assertEqual(list(self.root.iterdir()), [])
        with (
            mock.patch.object(Path, "unlink", side_effect=PermissionError("denied")),
            self.assertRaises(inference_config.InferenceConfigError),
        ):
            self.store.delete("team_1")


class SpaceResetTests(unittest.TestCase):
    def test_reset_removes_every_owned_file_even_without_a_team_network_and_keeps_foreign_ones(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "inference"
            store = inference_config.InferenceConfigStore(root)
            store.delete_all()
            store.save("team_1", inference_config.normalize("openai", "gpt-6-luna"))
            store.save_instructions("orphan_team", RULES)
            interrupted = root / f".{'a' * 64}.instructions.json.{'b' * 16}.tmp"
            interrupted.write_text("partial", encoding="utf-8")
            foreign = ["notes.txt", f".{'c' * 64}.json", f"{'c' * 64}.json.{'d' * 16}.tmp"]
            for name in foreign:
                (root / name).write_text("not ours", encoding="utf-8")
            store.delete_all()
            self.assertEqual(sorted(path.name for path in root.iterdir()), sorted(foreign))
            with (
                mock.patch.object(Path, "iterdir", side_effect=PermissionError("denied")),
                self.assertRaises(inference_config.InferenceConfigError),
            ):
                store.delete_all()


class ControllerTests(unittest.TestCase):
    def controller(self, directory: str) -> local_app.LocalController:
        controller = object.__new__(local_app.LocalController)
        controller._locks = tuple(threading.RLock() for _ in range(64))
        controller.inference_store = inference_config.InferenceConfigStore(Path(directory) / "inference")
        controller._wire_collaborators()
        controller.assistant_lifecycle._network = lambda _team_id: object()
        return controller

    def test_the_supervisor_replaces_and_reads_the_rules(self):
        with tempfile.TemporaryDirectory() as directory:
            controller = self.controller(directory)
            self.assertEqual(controller.instructions_status("team_1"), {"team_id": "team_1", "instructions": []})
            saved = controller.configure_instructions("team_1", {"instructions": RULES})
            self.assertEqual(saved, {"team_id": "team_1", "instructions": RULES})
            self.assertEqual(controller.instructions_status("team_1"), saved)

    def test_only_the_closed_body_with_valid_rules_is_admitted(self):
        with tempfile.TemporaryDirectory() as directory:
            controller = self.controller(directory)
            for body, code in (
                ([], "invalid-body"),
                ({"instructions": RULES, "extra": 1}, "invalid-body"),
                ({"instructions": ["a\nb"]}, "invalid-instructions"),
                ({"instructions": "one rule"}, "invalid-instructions"),
            ):
                with self.subTest(body=body), self.assertRaises(local_app.ApiProblem) as caught:
                    controller.configure_instructions("team_1", body)
                self.assertEqual(caught.exception.code, code)

    def test_store_failures_are_unavailable_not_invalid(self):
        with tempfile.TemporaryDirectory() as directory:
            controller = self.controller(directory)
            failure = inference_config.InferenceConfigError("disk")
            controller.inference_store = SimpleNamespace(
                load_instructions=mock.Mock(side_effect=failure),
                save_instructions=mock.Mock(side_effect=failure),
            )
            for call in (
                lambda: controller.instructions_status("team_1"),
                lambda: controller.configure_instructions("team_1", {"instructions": RULES}),
            ):
                with self.assertRaises(local_app.ApiProblem) as caught:
                    call()
                self.assertEqual(caught.exception.code, "inference-store-failed")

    def test_the_problem_helper_names_the_store(self):
        with self.assertRaises(local_app.ApiProblem) as caught:
            local_inference._raise_inference_problem(inference_config.InferenceConfigError("disk"))
        self.assertEqual(caught.exception.code, "inference-store-failed")


class ClientTests(RuntimeClientCase):
    def test_the_turn_request_carries_the_rules(self):
        client, connection = self.client(
            _Response({"status": "completed", "reply": "Oi.", "actions": [], "clarification": None})
        )
        client.start(dataclasses.replace(context(self.secret), instructions=tuple(RULES)), "Oi", conversation=())
        self.assertEqual(json.loads(connection.requests[0][2])["instructions"], RULES)


if __name__ == "__main__":
    unittest.main()
