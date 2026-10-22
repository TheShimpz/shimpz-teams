"""Learned Team memory (ADR-0084), saved only when a reply commits."""

import dataclasses
import json
import os
import stat
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
ZONE_THEN_RECORD = (
    chat_orchestrator.InvokedAction("shimpz-cloudflare", "list-zones"),
    chat_orchestrator.InvokedAction("shimpz-cloudflare", "ensure-dns-record"),
)


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

    def test_memory_changes_in_order_and_the_file_goes_when_nothing_is_left(self):
        self.assertEqual(self.store.load_knowledge("team_1"), [])
        self.assertEqual(
            self.store.apply_knowledge("team_1", [REMEMBER_LANGUAGE, {"op": "remember", **FORMAT}]), [LANGUAGE, FORMAT]
        )
        [stored] = self.root.glob("*.knowledge.json")
        self.assertEqual(stored.stat().st_mode & 0o777, 0o600)
        changed = {"op": "remember", "topic": "language", "preference": "Answer in English."}
        memory = self.store.apply_knowledge("team_1", [changed])
        self.assertEqual(memory, [FORMAT, {"topic": "language", "preference": "Answer in English."}])
        self.assertEqual(self.store.load_knowledge("team_2"), [])
        forget = [{"op": "forget", "topic": topic, "preference": ""} for topic in ("format", "language")]
        self.assertEqual(self.store.apply_knowledge("team_1", forget), [])
        self.assertEqual(list(self.root.glob("*.knowledge.json")), [])

    def test_forgetting_the_last_entry_commits_the_removal_or_reports_failure(self):
        real_fsync = os.fsync
        synced: list[bool] = []

        def observe(descriptor: int) -> None:
            real_fsync(descriptor)
            synced.append(stat.S_ISDIR(os.fstat(descriptor).st_mode))

        forget = [{"op": "forget", "topic": "language", "preference": ""}]
        self.store.apply_knowledge("team_1", [REMEMBER_LANGUAGE])
        with mock.patch.object(os, "fsync", side_effect=observe):
            self.assertEqual(self.store.apply_knowledge("team_1", forget), [])
        self.assertEqual(synced, [True])
        self.assertEqual(list(self.root.glob("*.knowledge.json")), [])

        self.store.apply_knowledge("team_1", [REMEMBER_LANGUAGE])
        with (
            mock.patch.object(os, "fsync", side_effect=OSError("directory sync")),
            self.assertRaisesRegex(inference_config.InferenceConfigError, "could not be removed"),
        ):
            self.store.apply_knowledge("team_1", forget)
        # A retry of the unconfirmed forget finds the record gone and still commits its directory entry.
        synced.clear()
        with mock.patch.object(os, "fsync", side_effect=observe):
            self.assertEqual(self.store.apply_knowledge("team_1", forget), [])
        self.assertEqual(synced, [True])

    def test_invalid_changes_and_team_ids_are_refused(self):
        for team_id, changes in (
            ("team_1", [{"op": "remember", "topic": "Bad", "preference": "x"}]),
            ("team_1", "not a list"),
            ("../team", [REMEMBER_LANGUAGE]),
        ):
            with self.subTest(changes=changes), self.assertRaises(inference_config.InferenceConfigError):
                self.store.apply_knowledge(team_id, changes)

    def test_a_failed_write_leaves_the_saved_knowledge_untouched(self):
        self.store.apply_knowledge("team_1", [REMEMBER_LANGUAGE])
        [stored] = self.root.glob("*.knowledge.json")
        before = stored.read_bytes()
        for failing in ("fchmod", "fsync"):
            with (
                self.subTest(failing=failing),
                mock.patch.object(os, failing, side_effect=OSError("disk")),
                self.assertRaises(inference_config.InferenceConfigError),
            ):
                self.store.apply_knowledge("team_1", [{"op": "remember", **FORMAT}])
            self.assertEqual(stored.read_bytes(), before)
            self.assertEqual(stored.stat().st_mode & 0o777, 0o600)
        self.assertEqual([path.name for path in self.root.iterdir()], [stored.name])

    def test_a_corrupt_or_foreign_file_fails_closed(self):
        self.store.apply_knowledge("team_1", [REMEMBER_LANGUAGE])
        [stored] = self.root.glob("*.knowledge.json")
        record = {"schema": inference_config.KNOWLEDGE_SCHEMA, "team_id": "team_1", "memory": [LANGUAGE]}
        for raw in (
            b"not json",
            b"\xff",
            json.dumps({**record, "team_id": "team_2"}).encode(),
            json.dumps({**record, "schema": inference_config.KNOWLEDGE_SCHEMA + 1}).encode(),
            json.dumps({**record, "memory": []}).encode(),
            json.dumps({**record, "memory": [dict(LANGUAGE, topic="Language")]}).encode(),
            json.dumps({**record, "extra": []}).encode(),
            json.dumps(["not", "an", "object"]).encode(),
        ):
            stored.write_bytes(raw)
            with self.subTest(raw=raw), self.assertRaises(inference_config.InferenceConfigError):
                self.store.load_knowledge("team_1")
        with (
            mock.patch("storage.private_state.os.open", side_effect=PermissionError("denied")),
            self.assertRaises(inference_config.InferenceConfigError),
        ):
            self.store.load_knowledge("team_1")

    def test_team_deletion_and_space_reset_remove_knowledge_and_keep_foreign_files(self):
        self.store.save("team_1", inference_config.normalize("openai", "gpt-6-luna"))
        self.store.apply_knowledge("team_1", [REMEMBER_LANGUAGE])
        self.store.delete("team_1")
        self.assertEqual(list(self.root.iterdir()), [])
        self.store.apply_knowledge("orphan_team", [REMEMBER_LANGUAGE])
        (self.root / f".{'a' * 64}.knowledge.json.{'b' * 16}.tmp").write_text("partial", encoding="utf-8")
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
        inference_config.InferenceConfigStore(self.root / "absent").delete_all()

    def test_one_turn_forgets_every_memory_together(self):
        memory = [{"op": "remember", "topic": f"t{index}", "preference": "x"} for index in range(32)]
        self.store.apply_knowledge("team_1", memory)
        memories = self.store.load_knowledge("team_1")
        self.assertEqual(len(memories), http_payload.MAX_MEMORIES)
        forget = _forget_all(entry["topic"] for entry in memories)
        self.assertEqual(len(forget), http_payload.MAX_MEMORY_CHANGES)
        self.assertEqual(self.store.apply_knowledge("team_1", forget), [])
        self.assertEqual(list(self.root.glob("*.knowledge.json")), [])
        with self.assertRaises(inference_config.InferenceConfigError):
            self.store.apply_knowledge("team_1", [*forget, dict(forget[0], topic="extra")])


def _forget_all(topics) -> list[dict[str, str]]:
    return [{"op": "forget", "topic": topic, "preference": ""} for topic in topics]


class ClientTests(RuntimeClientCase):
    def test_a_completed_turn_may_forget_every_memory_but_no_more(self):
        forget = _forget_all(f"t{index}" for index in range(http_payload.MAX_MEMORIES))
        payload = {"status": "completed", "reply": "Ok.", "actions": [], "clarification": None}
        client, _connection = self.client(_Response({**payload, "memory": forget}))
        self.assertEqual(client.start(context(self.secret), "Oi", conversation=()).memory, tuple(forget))
        client, _connection = self.client(_Response({**payload, "memory": [*forget, dict(forget[0], topic="extra")]}))
        with self.assertRaises(brain_runtime_client.BrainRuntimeError):
            client.start(context(self.secret), "Oi", conversation=())

    def test_the_turn_carries_knowledge_and_only_a_completed_turn_may_change_memory(self):
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
                chat_limit=SimpleNamespace(current=lambda: 2),
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

    def _complete(self, token: str, changes: tuple[dict[str, str], ...], actions=(), file_ids=()):
        outcome = chat_orchestrator.ChatOutcome(reply="Ok.", actions=tuple(actions), memory=changes)
        segment = chat_turn_engine.SegmentResult("Team", ("identity",), outcome, (), ())
        return self.service._segment_response(ResponseRequest("team_1", token, segment, (), file_ids, "openai"))

    def test_a_turn_that_consumed_selected_files_learns_nothing(self):
        with self.service._exclusive_chat_turn("team_1") as token:
            reply = self._complete(token, (REMEMBER_LANGUAGE,), ZONE_THEN_RECORD, file_ids=("a" * 32,))
        self.assertEqual(reply["reply"], "Ok.")
        self.assertEqual(self.store.load_knowledge("team_1"), [])

    def test_memory_is_saved_exactly_when_the_reply_commits(self):
        with self.service._exclusive_chat_turn("team_1") as token:
            self.assertEqual(self._complete(token, (REMEMBER_LANGUAGE,), ZONE_THEN_RECORD)["reply"], "Ok.")
        self.assertEqual(self.store.load_knowledge("team_1"), [LANGUAGE])
        with self.service._exclusive_chat_turn("team_1") as token:
            self._complete(token, (), ZONE_THEN_RECORD)
        self.assertEqual(self.store.load_knowledge("team_1"), [LANGUAGE])

    def test_a_stopped_turn_changes_nothing(self):
        with self.service._exclusive_chat_turn("team_1") as token:
            with self.service._active_chat_guard:
                self.service._cancelled_chat_tokens.add(token)
            with self.assertRaises(local_app.ApiProblem) as caught:
                self._complete(token, (REMEMBER_LANGUAGE,), ZONE_THEN_RECORD)
        self.assertEqual(caught.exception.code, "chat-stopped")
        self.assertEqual(self.store.load_knowledge("team_1"), [])

    def test_a_failed_audit_fails_the_turn_before_knowledge_changes(self):
        self.store.apply_knowledge("team_1", [{"op": "remember", **FORMAT}])
        with (
            mock.patch.object(local_audit, "record_request", side_effect=RuntimeError("audit down")),
            self.service._exclusive_chat_turn("team_1") as token,
        ):
            with self.assertRaisesRegex(RuntimeError, "audit down"):
                self._complete(token, (REMEMBER_LANGUAGE,), ZONE_THEN_RECORD)
            self.assertEqual(self.service._active_chat_tokens.get("team_1"), token)
        self.assertEqual(self.store.load_knowledge("team_1"), [FORMAT])

    def test_a_failed_save_whose_error_audit_also_fails_changes_nothing(self):
        self.store.apply_knowledge("team_1", [{"op": "remember", **FORMAT}])
        events = []

        def record(operation, **fields):
            events.append((operation, fields["result"], fields.get("detail")))
            if fields["result"] == "error":
                raise RuntimeError("audit down")
            return "trace"

        with (
            mock.patch.object(local_audit, "record_request", side_effect=record),
            mock.patch.object(self.store, "apply_knowledge", side_effect=inference_config.InferenceConfigError("disk")),
            self.service._exclusive_chat_turn("team_1") as token,
            self.assertRaisesRegex(RuntimeError, "audit down"),
        ):
            self._complete(token, (REMEMBER_LANGUAGE,), ZONE_THEN_RECORD)
        self.assertEqual(
            events,
            [
                ("chat-memory", "ok", "attempt:memory=1"),
                ("chat-memory", "error", "save-failed"),
            ],
        )
        self.assertEqual(self.store.load_knowledge("team_1"), [FORMAT])

    def test_a_failed_save_fails_the_turn_and_keeps_it_uncommitted(self):
        self.service.inference_store = SimpleNamespace(
            apply_knowledge=mock.Mock(side_effect=inference_config.InferenceConfigError("disk")),
        )
        with self.service._exclusive_chat_turn("team_1") as token:
            with self.assertRaises(local_app.ApiProblem) as caught:
                self._complete(token, (REMEMBER_LANGUAGE,))
            self.assertEqual(self.service._active_chat_tokens.get("team_1"), token)
        self.assertEqual(caught.exception.code, "memory-store-failed")


if __name__ == "__main__":
    unittest.main()
