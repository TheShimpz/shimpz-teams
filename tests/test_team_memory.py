"""Learned Team memory: private per Team, changed only when a reply commits, deleted with the Team (ADR-0084)."""

from __future__ import annotations

import dataclasses
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from test_brain_runtime_client import RuntimeClientCase, _Response, context

from chat import orchestrator as chat_orchestrator
from chat import turn as chat_turn_engine
from inference import client as brain_runtime_client
from inference import config as inference_config
from local import app as local_app
from local import audit as local_audit
from local.chat.types import ResponseRequest
from protocol.http.v1 import payload as http_payload

LANGUAGE = {"topic": "language", "preference": "Answer in Brazilian Portuguese."}
FORMAT = {"topic": "format", "preference": "Use short lists."}
REMEMBER_LANGUAGE = {"op": "remember", **LANGUAGE}


class ContractTests(unittest.TestCase):
    def test_memory_and_changes_admit_only_the_closed_shapes(self):
        self.assertEqual(http_payload.canonical_memory([LANGUAGE, FORMAT]), [LANGUAGE, FORMAT])
        for value in (
            None,
            [dict(LANGUAGE, topic="Language")],
            [dict(LANGUAGE, preference="")],
            [{"topic": "language"}],
            [LANGUAGE, dict(LANGUAGE, preference="Other.")],
            [{"topic": f"t{index}", "preference": "x"} for index in range(http_payload.MAX_MEMORIES + 1)],
        ):
            with self.subTest(value=value):
                self.assertIsNone(http_payload.canonical_memory(value))
        forget = {"op": "forget", "topic": "format", "preference": ""}
        self.assertEqual(
            http_payload.canonical_memory_changes([REMEMBER_LANGUAGE, forget]), [REMEMBER_LANGUAGE, forget]
        )
        for value in (
            None,
            [dict(REMEMBER_LANGUAGE, preference="")],
            [dict(forget, preference="x")],
            [dict(forget, op="replace")],
            [dict(REMEMBER_LANGUAGE, topic="Bad Topic")],
            [dict(REMEMBER_LANGUAGE, evidence="x")],
            [dict(REMEMBER_LANGUAGE, op=[])],
            ["remember"],
        ):
            with self.subTest(value=value):
                self.assertIsNone(http_payload.canonical_memory_changes(value))


class StoreTests(unittest.TestCase):
    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name) / "inference"
        self.store = inference_config.InferenceConfigStore(self.root)

    def test_changes_replace_forget_and_remove_the_file_when_nothing_is_left(self):
        self.assertEqual(self.store.load_memory("team_1"), [])
        self.assertEqual(
            self.store.apply_memory_changes("team_1", [REMEMBER_LANGUAGE, {"op": "remember", **FORMAT}]),
            [
                LANGUAGE,
                FORMAT,
            ],
        )
        [stored] = self.root.glob("*.memory.json")
        self.assertEqual(stored.stat().st_mode & 0o777, 0o600)
        changed = {"op": "remember", "topic": "language", "preference": "Answer in English."}
        self.assertEqual(
            self.store.apply_memory_changes("team_1", [changed]),
            [
                FORMAT,
                {"topic": "language", "preference": "Answer in English."},
            ],
        )
        self.assertEqual(self.store.load_memory("team_2"), [])
        forget = [{"op": "forget", "topic": topic, "preference": ""} for topic in ("format", "language")]
        self.assertEqual(self.store.apply_memory_changes("team_1", forget), [])
        self.assertEqual(list(self.root.glob("*.memory.json")), [])

    def test_a_failed_write_leaves_the_saved_memory_untouched(self):
        self.store.apply_memory_changes("team_1", [REMEMBER_LANGUAGE])
        [stored] = self.root.glob("*.memory.json")
        before = stored.read_bytes()
        for failing in ("fchmod", "fsync"):
            with (
                self.subTest(failing=failing),
                mock.patch.object(inference_config.os, failing, side_effect=OSError("disk")),
                self.assertRaises(inference_config.InferenceConfigError),
            ):
                self.store.apply_memory_changes("team_1", [{"op": "remember", **FORMAT}])
            self.assertEqual(stored.read_bytes(), before)
            self.assertEqual(stored.stat().st_mode & 0o777, 0o600)
        self.assertEqual([path.name for path in self.root.iterdir()], [stored.name])

    def test_invalid_changes_and_team_ids_are_refused(self):
        for team_id, changes in (
            ("team_1", [{"op": "remember", "topic": "Bad", "preference": "x"}]),
            ("team_1", "not a list"),
            ("../team", [REMEMBER_LANGUAGE]),
        ):
            with self.subTest(changes=changes), self.assertRaises(inference_config.InferenceConfigError):
                self.store.apply_memory_changes(team_id, changes)

    def test_a_corrupt_or_foreign_file_fails_closed(self):
        self.store.apply_memory_changes("team_1", [REMEMBER_LANGUAGE])
        [stored] = self.root.glob("*.memory.json")
        for raw in (
            b"not json",
            b"\xff",
            json.dumps({"schema": 1, "team_id": "team_2", "memory": [LANGUAGE]}).encode(),
            json.dumps({"schema": 2, "team_id": "team_1", "memory": [LANGUAGE]}).encode(),
            json.dumps({"schema": 1, "team_id": "team_1", "memory": []}).encode(),
            json.dumps(["not", "an", "object"]).encode(),
        ):
            stored.write_bytes(raw)
            with self.subTest(raw=raw), self.assertRaises(inference_config.InferenceConfigError):
                self.store.load_memory("team_1")
        with (
            mock.patch.object(Path, "read_bytes", side_effect=PermissionError("denied")),
            self.assertRaises(inference_config.InferenceConfigError),
        ):
            self.store.load_memory("team_1")

    def test_team_deletion_and_space_reset_remove_memory_and_keep_foreign_files(self):
        self.store.save("team_1", inference_config.normalize("openai", "gpt-6-luna"))
        self.store.apply_memory_changes("team_1", [REMEMBER_LANGUAGE])
        self.store.delete("team_1")
        self.assertEqual(list(self.root.iterdir()), [])
        self.store.apply_memory_changes("orphan_team", [REMEMBER_LANGUAGE])
        (self.root / f".{'a' * 64}.memory.json.{'b' * 16}.tmp").write_text("partial", encoding="utf-8")
        foreign = ["notes.txt", f".{'c' * 64}.json", f"{'c' * 64}.json.{'d' * 16}.tmp"]
        for name in foreign:
            (self.root / name).write_text("not ours", encoding="utf-8")
        self.store.delete_all()
        self.assertEqual(sorted(path.name for path in self.root.iterdir()), sorted(foreign))
        with (
            mock.patch.object(Path, "iterdir", side_effect=PermissionError("denied")),
            self.assertRaises(inference_config.InferenceConfigError),
        ):
            self.store.delete_all()
        with (
            mock.patch.object(Path, "unlink", side_effect=PermissionError("denied")),
            self.assertRaises(inference_config.InferenceConfigError),
        ):
            self.store.delete("team_1")
        missing = inference_config.InferenceConfigStore(self.root / "absent")
        missing.delete_all()


class ClientTests(RuntimeClientCase):
    def test_the_turn_carries_memory_and_only_a_completed_turn_may_change_it(self):
        payload = {"status": "completed", "reply": "Ok.", "actions": [], "clarification": None}
        client, connection = self.client(_Response({**payload, "memory": [REMEMBER_LANGUAGE]}))
        turn = client.start(dataclasses.replace(context(self.secret), memories=(LANGUAGE,)), "Oi", conversation=())
        self.assertEqual(turn.memory, (REMEMBER_LANGUAGE,))
        self.assertEqual(json.loads(connection.requests[0][2])["memories"], [LANGUAGE])
        client, connection = self.client(_Response(payload))
        client.start(context(self.secret), "Oi", conversation=())
        self.assertIsNone(json.loads(connection.requests[0][2])["memories"])
        suspended = {
            "status": "action-required",
            "reply": "",
            "actions": [{"interrupt_id": "i1", "assistant_id": "hello-pulse", "action": "hello", "input": {}}],
            "clarification": None,
            "memory": [REMEMBER_LANGUAGE],
        }
        for body in (suspended, {**payload, "memory": [{"op": "remember", "topic": "Bad", "preference": "x"}]}):
            client, _connection = self.client(_Response(body))
            with self.subTest(body=body), self.assertRaises(brain_runtime_client.BrainRuntimeError):
                client.start(context(self.secret), "Oi", conversation=())


class CommitTests(unittest.TestCase):
    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.store = inference_config.InferenceConfigStore(Path(directory.name) / "inference")
        self.service = local_app.ChatTurnService(
            local_app.ChatTurnDependencies(
                integration_challenges=SimpleNamespace(cancel_team=lambda _team_id: False),
                oauth_pkce=SimpleNamespace(cancel_team=lambda _team_id: None),
                inference_store=self.store,
            )
        )
        self.service._delete_chat_continuation = lambda _team_id: False
        local_audit.close()
        self.addCleanup(local_audit.close)
        audit_path = mock.patch.object(local_audit, "AUDIT_PATH", Path(directory.name) / "audit.jsonl")
        audit_path.start()
        self.addCleanup(audit_path.stop)
        principal = local_audit.bind_request_principal(local_audit.AuditPrincipal("admin", "machine"))
        principal.__enter__()
        self.addCleanup(principal.__exit__, None, None, None)

    def _complete(self, token: str, changes: tuple[dict[str, str], ...]):
        outcome = chat_orchestrator.ChatOutcome(reply="Ok.", actions=(), memory=changes)
        segment = chat_turn_engine.SegmentResult("Team", ("identity",), outcome, (), ())
        return self.service._segment_response(ResponseRequest("team_1", token, segment, (), (), "openai"))

    def test_changes_are_saved_exactly_when_the_reply_commits(self):
        with self.service._exclusive_chat_turn("team_1") as token:
            self.assertEqual(self._complete(token, (REMEMBER_LANGUAGE,))["reply"], "Ok.")
        self.assertEqual(self.store.load_memory("team_1"), [LANGUAGE])

    def test_a_stopped_turn_changes_nothing(self):
        with self.service._exclusive_chat_turn("team_1") as token:
            self.service.stop_chat = None
            with self.service._active_chat_guard:
                self.service._cancelled_chat_tokens.add(token)
            with self.assertRaises(local_app.ApiProblem) as caught:
                self._complete(token, (REMEMBER_LANGUAGE,))
        self.assertEqual(caught.exception.code, "chat-stopped")
        self.assertEqual(self.store.load_memory("team_1"), [])

    def test_a_failed_audit_fails_the_turn_before_memory_changes(self):
        self.store.apply_memory_changes("team_1", [{"op": "remember", **FORMAT}])
        with (
            mock.patch.object(local_audit, "record_request", side_effect=RuntimeError("audit down")),
            self.service._exclusive_chat_turn("team_1") as token,
        ):
            with self.assertRaisesRegex(RuntimeError, "audit down"):
                self._complete(token, (REMEMBER_LANGUAGE,))
            self.assertEqual(self.service._active_chat_tokens.get("team_1"), token)
        self.assertEqual(self.store.load_memory("team_1"), [FORMAT])

    def test_a_failed_save_whose_error_audit_also_fails_changes_nothing(self):
        self.store.apply_memory_changes("team_1", [{"op": "remember", **FORMAT}])
        events = []

        def record(operation, **fields):
            events.append((operation, fields["result"], fields.get("detail")))
            if fields["result"] == "error":
                raise RuntimeError("audit down")
            return "trace"

        with (
            mock.patch.object(local_audit, "record_request", side_effect=record),
            mock.patch.object(
                self.store, "apply_memory_changes", side_effect=inference_config.InferenceConfigError("disk")
            ),
            self.service._exclusive_chat_turn("team_1") as token,
            self.assertRaisesRegex(RuntimeError, "audit down"),
        ):
            self._complete(token, (REMEMBER_LANGUAGE,))
        self.assertEqual(events, [("chat-memory", "ok", "attempt:1"), ("chat-memory", "error", "save-failed")])
        self.assertEqual(self.store.load_memory("team_1"), [FORMAT])

    def test_a_failed_save_fails_the_turn_and_keeps_it_uncommitted(self):
        self.service.inference_store = SimpleNamespace(
            load_memory=lambda _team_id: [],
            apply_memory_changes=mock.Mock(side_effect=inference_config.InferenceConfigError("disk")),
        )
        with self.service._exclusive_chat_turn("team_1") as token:
            with self.assertRaises(local_app.ApiProblem) as caught:
                self._complete(token, (REMEMBER_LANGUAGE,))
            self.assertEqual(self.service._active_chat_tokens.get("team_1"), token)
        self.assertEqual(caught.exception.code, "memory-store-failed")


if __name__ == "__main__":
    unittest.main()
