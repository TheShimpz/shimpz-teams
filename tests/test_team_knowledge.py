"""Learned Team knowledge: memory (ADR-0084) and structure-only skills (ADR-0085), saved only when a reply commits."""

from __future__ import annotations

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

from chat import knowledge as chat_knowledge
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
CONTRACT = "sha256:" + "c" * 64


def _invoked(action: str, *inputs: str, assistant_id: str = "shimpz-cloudflare", contract: str = CONTRACT):
    return chat_orchestrator.InvokedAction(assistant_id, action, tuple(inputs), contract)


ZONE_THEN_RECORD = (_invoked("list-zones"), _invoked("ensure-dns-record", "content", "name", "type", "zone_id"))
SKILL = chat_knowledge.learned_skill(ZONE_THEN_RECORD)


class ContractTests(unittest.TestCase):
    def test_memory_and_changes_admit_only_the_closed_shapes(self):
        self.assertEqual(http_payload.canonical_memory([LANGUAGE, FORMAT]), [LANGUAGE, FORMAT])
        for value in (
            None,
            [dict(LANGUAGE, topic="Language")],
            [dict(LANGUAGE, preference="")],
            [{"topic": "language"}],
            [LANGUAGE, dict(LANGUAGE, preference="Other.")],
            [dict(LANGUAGE, topic="procedure-0123456789ab")],
            [{"topic": f"t{index}", "preference": "x"} for index in range(http_payload.MAX_MEMORIES + 1)],
        ):
            with self.subTest(value=value):
                self.assertIsNone(http_payload.canonical_memory(value))
        forget = {"op": "forget", "topic": "format", "preference": ""}
        forget_skill = {"op": "forget", "topic": SKILL["key"], "preference": ""}
        self.assertEqual(
            http_payload.canonical_memory_changes([REMEMBER_LANGUAGE, forget, forget_skill]),
            [REMEMBER_LANGUAGE, forget, forget_skill],
        )
        for value in (
            None,
            [dict(REMEMBER_LANGUAGE, preference="")],
            [dict(forget, preference="x")],
            [dict(forget, op="replace")],
            [dict(REMEMBER_LANGUAGE, topic="Bad Topic")],
            [dict(REMEMBER_LANGUAGE, topic=SKILL["key"])],
            [dict(REMEMBER_LANGUAGE, evidence="x")],
            [dict(REMEMBER_LANGUAGE, op=[])],
            ["remember"],
        ):
            with self.subTest(value=value):
                self.assertIsNone(http_payload.canonical_memory_changes(value))

    def test_a_skill_is_structure_bound_to_its_contracts(self):
        self.assertEqual(SKILL["contracts"], {"shimpz-cloudflare": CONTRACT})
        self.assertEqual([step["action"] for step in SKILL["steps"]], ["list-zones", "ensure-dns-record"])
        self.assertEqual(SKILL["key"], http_payload.skill_key(SKILL["contracts"], SKILL["steps"]))
        # Repeated steps are kept: two searches can both be essential.
        search = _invoked("search-web", "query", assistant_id="shimpz-exa")
        self.assertEqual(len(chat_knowledge.learned_skill((search, search))["steps"]), 2)
        self.assertIsNone(chat_knowledge.learned_skill(ZONE_THEN_RECORD[:1]))
        self.assertIsNone(chat_knowledge.learned_skill((_invoked("list-zones"),) * 17))
        drifted = (ZONE_THEN_RECORD[0], _invoked("ensure-dns-record", contract="sha256:" + "d" * 64))
        self.assertIsNone(chat_knowledge.learned_skill(drifted))
        unlearnable = (ZONE_THEN_RECORD[0], dataclasses.replace(ZONE_THEN_RECORD[1], learnable=False))
        self.assertIsNone(chat_knowledge.learned_skill(unlearnable))
        step = SKILL["steps"][0]
        for broken in (
            dict(SKILL, extra=1),
            dict(SKILL, contracts=[]),
            dict(SKILL, steps=SKILL["steps"][:1]),
            dict(SKILL, steps=["list-zones", "ensure-dns-record"]),
            dict(SKILL, steps=[dict(step, inputs=["b", "a"]), SKILL["steps"][1]]),
            dict(SKILL, steps=[dict(step, action="Bad Action"), SKILL["steps"][1]]),
            dict(SKILL, contracts={"other-assistant": CONTRACT}),
            dict(SKILL, contracts={"shimpz-cloudflare": "sha256:short"}),
        ):
            with self.subTest(broken=broken):
                self.assertIsNone(http_payload.canonical_skill(broken))
        for value in (None, [SKILL, SKILL], [dict(SKILL, key="procedure-000000000000")], "skills"):
            with self.subTest(value=value):
                self.assertIsNone(http_payload.canonical_skills(value))

    def test_every_stored_skill_reaches_brain_marked_usable_only_under_its_contracts(self):
        assistant = brain_runtime_client.RuntimeAssistant("shimpz-cloudflare", "DNS.", ())
        digest = brain_runtime_client.contract_digest(assistant)
        current = chat_knowledge.learned_skill(
            tuple(dataclasses.replace(action, contract=digest) for action in ZONE_THEN_RECORD)
        )
        self.assertEqual(
            chat_knowledge.turn_skills([SKILL, current], (assistant,)),
            ({**SKILL, "usable": False}, {**current, "usable": True}),
        )

    def test_a_skill_forgotten_in_a_turn_is_not_relearned_by_it_and_only_eight_are_kept(self):
        forget = [{"op": "forget", "topic": SKILL["key"], "preference": ""}]
        self.assertEqual(http_payload.apply_knowledge([], [SKILL], forget, SKILL), ([], []))
        many = [chat_knowledge.learned_skill((_invoked(f"a{index}"), _invoked("b"))) for index in range(10)]
        kept = []
        for skill in many:
            _memory, kept = http_payload.apply_knowledge([], kept, [], skill)
        self.assertEqual(kept, many[-http_payload.MAX_SKILLS :])


class StoreTests(unittest.TestCase):
    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name) / "inference"
        self.store = inference_config.InferenceConfigStore(self.root)

    def test_memory_and_skills_change_together_and_the_file_goes_when_nothing_is_left(self):
        self.assertEqual(self.store.load_knowledge("team_1"), ([], []))
        self.assertEqual(
            self.store.apply_knowledge("team_1", [REMEMBER_LANGUAGE, {"op": "remember", **FORMAT}], SKILL),
            ([LANGUAGE, FORMAT], [SKILL]),
        )
        [stored] = self.root.glob("*.knowledge.json")
        self.assertEqual(stored.stat().st_mode & 0o777, 0o600)
        changed = {"op": "remember", "topic": "language", "preference": "Answer in English."}
        memory, skills = self.store.apply_knowledge("team_1", [changed], None)
        self.assertEqual(memory, [FORMAT, {"topic": "language", "preference": "Answer in English."}])
        self.assertEqual(skills, [SKILL])
        self.assertEqual(self.store.load_knowledge("team_2"), ([], []))
        forget = [{"op": "forget", "topic": topic, "preference": ""} for topic in ("format", "language", SKILL["key"])]
        self.assertEqual(self.store.apply_knowledge("team_1", forget, None), ([], []))
        self.assertEqual(list(self.root.glob("*.knowledge.json")), [])

    def test_forgetting_the_last_entry_commits_the_removal_or_reports_failure(self):
        real_fsync = inference_config.os.fsync
        synced: list[bool] = []

        def observe(descriptor: int) -> None:
            real_fsync(descriptor)
            synced.append(stat.S_ISDIR(os.fstat(descriptor).st_mode))

        forget = [{"op": "forget", "topic": "language", "preference": ""}]
        self.store.apply_knowledge("team_1", [REMEMBER_LANGUAGE], None)
        with mock.patch.object(inference_config.os, "fsync", side_effect=observe):
            self.assertEqual(self.store.apply_knowledge("team_1", forget, None), ([], []))
        self.assertEqual(synced, [True])
        self.assertEqual(list(self.root.glob("*.knowledge.json")), [])

        self.store.apply_knowledge("team_1", [REMEMBER_LANGUAGE], None)
        with (
            mock.patch.object(inference_config.os, "fsync", side_effect=OSError("directory sync")),
            self.assertRaisesRegex(inference_config.InferenceConfigError, "could not be removed"),
        ):
            self.store.apply_knowledge("team_1", forget, None)
        # A retry of the unconfirmed forget finds the record gone and still commits its directory entry.
        synced.clear()
        with mock.patch.object(inference_config.os, "fsync", side_effect=observe):
            self.assertEqual(self.store.apply_knowledge("team_1", forget, None), ([], []))
        self.assertEqual(synced, [True])

    def test_invalid_changes_skills_and_team_ids_are_refused(self):
        for team_id, changes, skill in (
            ("team_1", [{"op": "remember", "topic": "Bad", "preference": "x"}], None),
            ("team_1", "not a list", None),
            ("team_1", [], dict(SKILL, key="procedure-000000000000")),
            ("../team", [REMEMBER_LANGUAGE], None),
        ):
            with self.subTest(changes=changes, skill=skill), self.assertRaises(inference_config.InferenceConfigError):
                self.store.apply_knowledge(team_id, changes, skill)

    def test_a_failed_write_leaves_the_saved_knowledge_untouched(self):
        self.store.apply_knowledge("team_1", [REMEMBER_LANGUAGE], None)
        [stored] = self.root.glob("*.knowledge.json")
        before = stored.read_bytes()
        for failing in ("fchmod", "fsync"):
            with (
                self.subTest(failing=failing),
                mock.patch.object(inference_config.os, failing, side_effect=OSError("disk")),
                self.assertRaises(inference_config.InferenceConfigError),
            ):
                self.store.apply_knowledge("team_1", [{"op": "remember", **FORMAT}], SKILL)
            self.assertEqual(stored.read_bytes(), before)
            self.assertEqual(stored.stat().st_mode & 0o777, 0o600)
        self.assertEqual([path.name for path in self.root.iterdir()], [stored.name])

    def test_a_corrupt_or_foreign_file_fails_closed(self):
        self.store.apply_knowledge("team_1", [REMEMBER_LANGUAGE], None)
        [stored] = self.root.glob("*.knowledge.json")
        record = {"schema": 1, "team_id": "team_1", "memory": [LANGUAGE], "skills": []}
        for raw in (
            b"not json",
            b"\xff",
            json.dumps({**record, "team_id": "team_2"}).encode(),
            json.dumps({**record, "schema": 2}).encode(),
            json.dumps({**record, "memory": []}).encode(),
            json.dumps({**record, "skills": [dict(SKILL, key="procedure-000000000000")]}).encode(),
            json.dumps({"schema": 1, "team_id": "team_1", "memory": [LANGUAGE]}).encode(),
            json.dumps(["not", "an", "object"]).encode(),
        ):
            stored.write_bytes(raw)
            with self.subTest(raw=raw), self.assertRaises(inference_config.InferenceConfigError):
                self.store.load_knowledge("team_1")
        with (
            mock.patch.object(Path, "read_bytes", side_effect=PermissionError("denied")),
            self.assertRaises(inference_config.InferenceConfigError),
        ):
            self.store.load_knowledge("team_1")

    def test_team_deletion_and_space_reset_remove_knowledge_and_keep_foreign_files(self):
        self.store.save("team_1", inference_config.normalize("openai", "gpt-6-luna"))
        self.store.apply_knowledge("team_1", [REMEMBER_LANGUAGE], SKILL)
        self.store.delete("team_1")
        self.assertEqual(list(self.root.iterdir()), [])
        self.store.apply_knowledge("orphan_team", [REMEMBER_LANGUAGE], None)
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


class ClientTests(RuntimeClientCase):
    def test_the_turn_carries_knowledge_and_only_a_completed_turn_may_change_memory(self):
        payload = {"status": "completed", "reply": "Ok.", "actions": [], "clarification": None}
        client, connection = self.client(_Response({**payload, "memory": [REMEMBER_LANGUAGE]}))
        turn = client.start(
            dataclasses.replace(context(self.secret), memories=(LANGUAGE,), skills=(SKILL,)), "Oi", conversation=()
        )
        self.assertEqual(turn.memory, (REMEMBER_LANGUAGE,))
        sent = json.loads(connection.requests[0][2])
        self.assertEqual((sent["memories"], sent["skills"]), ([LANGUAGE], [SKILL]))
        client, connection = self.client(_Response(payload))
        client.start(context(self.secret), "Oi", conversation=())
        sent = json.loads(connection.requests[0][2])
        self.assertEqual((sent["memories"], sent["skills"]), (None, None))
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

    def _complete(self, token: str, changes: tuple[dict[str, str], ...], actions=()):
        outcome = chat_orchestrator.ChatOutcome(reply="Ok.", actions=tuple(actions), memory=changes)
        segment = chat_turn_engine.SegmentResult("Team", ("identity",), outcome, (), ())
        return self.service._segment_response(ResponseRequest("team_1", token, segment, (), (), "openai"))

    def test_memory_and_the_learned_skill_are_saved_exactly_when_the_reply_commits(self):
        with self.service._exclusive_chat_turn("team_1") as token:
            self.assertEqual(self._complete(token, (REMEMBER_LANGUAGE,), ZONE_THEN_RECORD)["reply"], "Ok.")
        self.assertEqual(self.store.load_knowledge("team_1"), ([LANGUAGE], [SKILL]))
        with self.service._exclusive_chat_turn("team_1") as token:
            self._complete(token, (), ZONE_THEN_RECORD[:1])
        self.assertEqual(self.store.load_knowledge("team_1"), ([LANGUAGE], [SKILL]))

    def test_a_stopped_turn_changes_nothing(self):
        with self.service._exclusive_chat_turn("team_1") as token:
            with self.service._active_chat_guard:
                self.service._cancelled_chat_tokens.add(token)
            with self.assertRaises(local_app.ApiProblem) as caught:
                self._complete(token, (REMEMBER_LANGUAGE,), ZONE_THEN_RECORD)
        self.assertEqual(caught.exception.code, "chat-stopped")
        self.assertEqual(self.store.load_knowledge("team_1"), ([], []))

    def test_a_failed_audit_fails_the_turn_before_knowledge_changes(self):
        self.store.apply_knowledge("team_1", [{"op": "remember", **FORMAT}], None)
        with (
            mock.patch.object(local_audit, "record_request", side_effect=RuntimeError("audit down")),
            self.service._exclusive_chat_turn("team_1") as token,
        ):
            with self.assertRaisesRegex(RuntimeError, "audit down"):
                self._complete(token, (REMEMBER_LANGUAGE,), ZONE_THEN_RECORD)
            self.assertEqual(self.service._active_chat_tokens.get("team_1"), token)
        self.assertEqual(self.store.load_knowledge("team_1"), ([FORMAT], []))

    def test_a_failed_save_whose_error_audit_also_fails_changes_nothing(self):
        self.store.apply_knowledge("team_1", [{"op": "remember", **FORMAT}], None)
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
                ("chat-memory", "ok", "attempt:memory=1,skill=1"),
                ("chat-memory", "error", "save-failed"),
            ],
        )
        self.assertEqual(self.store.load_knowledge("team_1"), ([FORMAT], []))

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
