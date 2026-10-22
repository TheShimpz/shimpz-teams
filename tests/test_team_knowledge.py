"""Learned Team memory (ADR-0084) and its private provenance (ADR-0121), saved only when a reply commits."""

import dataclasses
import json
import os
import stat
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from test_brain_runtime_client import RuntimeClientCase, _Response, context

from chat import orchestrator as chat_orchestrator
from chat import turn as chat_turn_engine
from inference import client as brain_runtime_client
from inference import config as inference_config
from inference import memory as inference_memory
from local import app as local_app
from local import audit as local_audit
from local.chat import api as local_chat_api
from local.chat import memory as local_chat_memory
from local.chat import pause as local_chat_pause
from local.chat.types import ResponseRequest
from protocol.http.v1 import payload as http_payload

LANGUAGE = {"topic": "language", "preference": "Answer in Brazilian Portuguese."}
FORMAT = {"topic": "format", "preference": "Use short lists."}
REMEMBER_LANGUAGE = {"op": "remember", **LANGUAGE}
AUTHOR = "a" * 32
RESUMER = "d" * 32
SOURCE = inference_memory.Source("b" * 32, AUTHOR, "c" * 64)
OTHER_SOURCE = inference_memory.Source("e" * 32, RESUMER, "c" * 64)
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

    def apply(self, team_id: str, changes: object, source: object = SOURCE) -> inference_memory.Plan:
        plan = self.store.plan_knowledge(team_id, changes, source)
        self.store.save_knowledge(team_id, plan)
        return plan

    def entries(self, team_id: str = "team_1") -> list[dict[str, object]]:
        [stored] = self.root.glob("*.knowledge.json")
        record = json.loads(stored.read_bytes())
        self.assertEqual(record["team_id"], team_id)
        return record["memory"]

    def test_a_remembered_entry_is_stamped_and_the_turn_sees_only_topic_and_preference(self):
        self.assertEqual(self.store.load_knowledge("team_1"), [])
        plan = self.apply("team_1", [REMEMBER_LANGUAGE, {"op": "remember", **FORMAT}])
        [stored] = self.root.glob("*.knowledge.json")
        self.assertEqual(stored.stat().st_mode & 0o777, 0o600)
        language, format_ = self.entries()
        self.assertRegex(language["memory_id"], r"\A[0-9a-f]{32}\Z")
        self.assertNotEqual(language["memory_id"], format_["memory_id"])
        self.assertRegex(language["since"], r"\A[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z\Z")
        self.assertEqual(language["source"], {"nonce": "b" * 32, "actor_id": AUTHOR, "incarnation": "c" * 64})
        self.assertEqual(
            plan.changes,
            (
                inference_memory.Change(inference_memory.REMEMBER, language["memory_id"]),
                inference_memory.Change(inference_memory.REMEMBER, format_["memory_id"]),
            ),
        )
        # The turn context, and so the Brain, never receives an id, a time, or a source.
        self.assertEqual(self.store.load_knowledge("team_1"), [LANGUAGE, FORMAT])
        self.assertEqual(self.store.load_knowledge("team_2"), [])

    def test_a_reused_topic_gets_a_new_identity_time_and_source_and_forget_removes_it_whole(self):
        self.apply("team_1", [REMEMBER_LANGUAGE, {"op": "remember", **FORMAT}])
        language, format_ = self.entries()
        changed = {"op": "remember", "topic": "language", "preference": "Answer in English."}
        with mock.patch.object(inference_config.time, "gmtime", return_value=time.gmtime(4_102_444_800)):
            plan = self.apply("team_1", [changed], OTHER_SOURCE)
        kept, replaced = self.entries()
        self.assertEqual(kept, format_)
        self.assertNotEqual(replaced["memory_id"], language["memory_id"])
        self.assertEqual(replaced["since"], "2100-01-01T00:00:00Z")
        self.assertEqual(replaced["source"], inference_memory.source_record(OTHER_SOURCE))
        self.assertEqual(plan.changes, (inference_memory.Change(inference_memory.REMEMBER, replaced["memory_id"]),))
        self.assertEqual(
            self.store.load_knowledge("team_1"), [FORMAT, {"topic": "language", "preference": "Answer in English."}]
        )
        forget = _forget_all(("format", "absent"))
        plan = self.apply("team_1", forget)
        self.assertEqual(plan.changes, (inference_memory.Change(inference_memory.FORGET, format_["memory_id"]),))
        self.assertEqual(self.entries(), [replaced])
        self.apply("team_1", _forget_all(("language",)))
        self.assertEqual(list(self.root.glob("*.knowledge.json")), [])

    def test_the_bound_evicts_the_oldest_entry_and_names_it(self):
        self.apply("team_1", [{"op": "remember", "topic": f"t{index}", "preference": "x"} for index in range(32)])
        oldest = self.entries()[0]
        plan = self.apply("team_1", [{"op": "remember", "topic": "new", "preference": "y"}])
        newest = self.entries()[-1]
        self.assertEqual(
            plan.changes,
            (
                inference_memory.Change(inference_memory.REMEMBER, newest["memory_id"]),
                inference_memory.Change(inference_memory.EVICTED, oldest["memory_id"]),
            ),
        )
        self.assertNotIn(oldest["memory_id"], [entry["memory_id"] for entry in self.entries()])

    def test_the_plan_applies_changes_exactly_as_the_protocol_vectors(self):
        vectors = json.loads((Path(http_payload.__file__).with_name("vectors.json")).read_text(encoding="utf-8"))
        for vector in vectors["memory_apply"]:
            entries = [
                {**entry, "memory_id": f"{index:032x}", "since": "2026-10-21T00:00:00Z", "source": {}}
                for index, entry in enumerate(vector["memory"])
            ]
            plan = inference_memory.plan(entries, vector["changes"], SOURCE, "2026-10-21T00:00:01Z")
            with self.subTest(changes=vector["changes"]):
                self.assertEqual(inference_memory.projection(list(plan.memory)), vector["result"])
                self.assertEqual(
                    http_payload.apply_memory_changes(vector["memory"], vector["changes"]), vector["result"]
                )

    def test_forgetting_the_last_entry_commits_the_removal_or_reports_failure(self):
        real_fsync = os.fsync
        synced: list[bool] = []

        def observe(descriptor: int) -> None:
            real_fsync(descriptor)
            synced.append(stat.S_ISDIR(os.fstat(descriptor).st_mode))

        forget = _forget_all(("language",))
        self.apply("team_1", [REMEMBER_LANGUAGE])
        with mock.patch.object(os, "fsync", side_effect=observe):
            self.apply("team_1", forget)
        self.assertEqual(synced, [True])
        self.assertEqual(list(self.root.glob("*.knowledge.json")), [])

        self.apply("team_1", [REMEMBER_LANGUAGE])
        plan = self.store.plan_knowledge("team_1", forget, SOURCE)
        with (
            mock.patch.object(os, "fsync", side_effect=OSError("directory sync")),
            self.assertRaisesRegex(inference_config.InferenceConfigError, "could not be removed"),
        ):
            self.store.save_knowledge("team_1", plan)
        # A retry of the unconfirmed forget finds the record gone and still commits its directory entry.
        synced.clear()
        with mock.patch.object(os, "fsync", side_effect=observe):
            self.store.save_knowledge("team_1", plan)
        self.assertEqual(synced, [True])

    def test_invalid_changes_sources_and_team_ids_are_refused(self):
        for team_id, changes, source in (
            ("team_1", [{"op": "remember", "topic": "Bad", "preference": "x"}], SOURCE),
            ("team_1", "not a list", SOURCE),
            ("team_1", [REMEMBER_LANGUAGE], None),
            ("team_1", [REMEMBER_LANGUAGE], inference_memory.Source("b" * 32, AUTHOR, "network")),
            ("team_1", [REMEMBER_LANGUAGE], {"nonce": "b" * 32, "actor_id": "A" * 32, "incarnation": "c" * 64}),
            ("../team", [REMEMBER_LANGUAGE], SOURCE),
        ):
            with self.subTest(changes=changes, source=source), self.assertRaises(inference_config.InferenceConfigError):
                self.store.plan_knowledge(team_id, changes, source)
        with self.assertRaises(inference_config.InferenceConfigError):
            self.store.save_knowledge("../team", inference_memory.Plan((), ()))

    def test_a_failed_write_leaves_the_saved_knowledge_untouched(self):
        self.apply("team_1", [REMEMBER_LANGUAGE])
        [stored] = self.root.glob("*.knowledge.json")
        before = stored.read_bytes()
        for failing in ("fchmod", "fsync"):
            plan = self.store.plan_knowledge("team_1", [{"op": "remember", **FORMAT}], SOURCE)
            with (
                self.subTest(failing=failing),
                mock.patch.object(os, failing, side_effect=OSError("disk")),
                self.assertRaises(inference_config.InferenceConfigError),
            ):
                self.store.save_knowledge("team_1", plan)
            self.assertEqual(stored.read_bytes(), before)
            self.assertEqual(stored.stat().st_mode & 0o777, 0o600)
        self.assertEqual([path.name for path in self.root.iterdir()], [stored.name])

    def test_a_corrupt_or_foreign_file_fails_closed(self):
        self.apply("team_1", [REMEMBER_LANGUAGE])
        [stored] = self.root.glob("*.knowledge.json")
        [entry] = self.entries()
        record = {"schema": inference_config.KNOWLEDGE_SCHEMA, "team_id": "team_1", "memory": [entry]}
        other = dict(entry, topic="format")
        for raw in (
            b"not json",
            b"\xff",
            json.dumps({**record, "team_id": "team_2"}).encode(),
            json.dumps({**record, "schema": inference_config.KNOWLEDGE_SCHEMA + 1}).encode(),
            json.dumps({**record, "memory": []}).encode(),
            json.dumps({**record, "memory": [LANGUAGE]}).encode(),
            json.dumps({**record, "memory": [dict(entry, topic="Language")]}).encode(),
            json.dumps({**record, "memory": [dict(entry, memory_id="A" * 32)]}).encode(),
            json.dumps({**record, "memory": [dict(entry, memory_id=7)]}).encode(),
            json.dumps({**record, "memory": [dict(entry, since="2026-10-21 00:00:00")]}).encode(),
            json.dumps({**record, "memory": [dict(entry, since=None)]}).encode(),
            json.dumps({**record, "memory": [dict(entry, source={**entry["source"], "nonce": "x"})]}).encode(),
            json.dumps({**record, "memory": [dict(entry, source={**entry["source"], "incarnation": 7})]}).encode(),
            json.dumps({**record, "memory": [dict(entry, source={**entry["source"], "extra": "x"})]}).encode(),
            json.dumps({**record, "memory": [entry, other]}).encode(),
            json.dumps({**record, "memory": "not a list"}).encode(),
            json.dumps({**record, "memory": ["not an entry"]}).encode(),
            json.dumps({**record, "extra": []}).encode(),
            json.dumps(["not", "an", "object"]).encode(),
        ):
            stored.write_bytes(raw)
            with self.subTest(raw=raw), self.assertRaises(inference_config.InferenceConfigError):
                self.store.load_knowledge("team_1")
        stored.write_bytes(json.dumps({**record, "memory": [entry, dict(other, memory_id="f" * 32)]}).encode())
        self.assertEqual(
            self.store.load_knowledge("team_1"), [LANGUAGE, {"topic": "format", "preference": LANGUAGE["preference"]}]
        )
        with (
            mock.patch("storage.private_state.os.open", side_effect=PermissionError("denied")),
            self.assertRaises(inference_config.InferenceConfigError),
        ):
            self.store.load_knowledge("team_1")

    def test_team_deletion_and_space_reset_remove_knowledge_and_keep_foreign_files(self):
        self.store.save("team_1", inference_config.normalize("openai", "gpt-6-luna"))
        self.apply("team_1", [REMEMBER_LANGUAGE])
        self.store.delete("team_1")
        self.assertEqual(list(self.root.iterdir()), [])
        self.apply("orphan_team", [REMEMBER_LANGUAGE])
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
        self.apply("team_1", [{"op": "remember", "topic": f"t{index}", "preference": "x"} for index in range(32)])
        memories = self.store.load_knowledge("team_1")
        self.assertEqual(len(memories), http_payload.MAX_MEMORIES)
        forget = _forget_all(entry["topic"] for entry in memories)
        self.assertEqual(len(forget), http_payload.MAX_MEMORY_CHANGES)
        plan = self.apply("team_1", forget)
        self.assertEqual({change.operation for change in plan.changes}, {inference_memory.FORGET})
        self.assertEqual(list(self.root.glob("*.knowledge.json")), [])
        with self.assertRaises(inference_config.InferenceConfigError):
            self.store.plan_knowledge("team_1", [*forget, dict(forget[0], topic="extra")], SOURCE)


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
        self.audit_path = Path(directory.name) / "audit.jsonl"
        audit_path = mock.patch.object(local_audit, "AUDIT_PATH", self.audit_path)
        audit_path.start()
        self.addCleanup(audit_path.stop)
        # The request that commits is another person's: only the turn's captured source ever names the author.
        principal = local_audit.bind_request_principal(local_audit.AuditPrincipal(RESUMER, "human"))
        principal.__enter__()
        self.addCleanup(principal.__exit__, None, None, None)

    def remember(self, *changes: dict[str, str]) -> None:
        self.store.save_knowledge("team_1", self.store.plan_knowledge("team_1", list(changes), OTHER_SOURCE))

    def _complete(self, token: str, changes: tuple[dict[str, str], ...], actions=(), file_ids=(), source=SOURCE):
        outcome = chat_orchestrator.ChatOutcome(reply="Ok.", actions=tuple(actions), memory=changes)
        segment = chat_turn_engine.SegmentResult("Team", ("identity",), outcome, (), ())
        return self.service._segment_response(
            ResponseRequest("team_1", token, segment, (), file_ids, "openai", source=source)
        )

    def activity(self) -> list[dict[str, object]]:
        if not self.audit_path.exists():
            return []
        lines = [json.loads(line) for line in self.audit_path.read_text(encoding="utf-8").splitlines()]
        return [line for line in lines if "activity" in line]

    def phases(self) -> list[tuple[str, str, str]]:
        return [(line["operation"], line["activity"]["phase"], line["result"]) for line in self.activity()]

    def test_each_change_is_requested_then_completed_by_team_for_the_turns_author(self):
        self.remember({"op": "remember", **FORMAT})
        [format_id] = [entry["memory_id"] for entry in self.store._memory("team_1")]
        forget = {"op": "forget", "topic": "format", "preference": ""}
        with self.service._exclusive_chat_turn("team_1") as token:
            self.assertEqual(self._complete(token, (REMEMBER_LANGUAGE, forget), ZONE_THEN_RECORD)["reply"], "Ok.")
        [language] = self.store._memory("team_1")
        self.assertEqual(language["source"], inference_memory.source_record(SOURCE))
        self.assertEqual(
            self.phases(),
            [
                ("memory-remember", "requested", "ok"),
                ("memory-forget", "requested", "ok"),
                ("memory-remember", "completed", "ok"),
                ("memory-forget", "completed", "ok"),
            ],
        )
        events = self.activity()
        self.assertEqual([line["activity"]["subject"] for line in events], [language["memory_id"], format_id] * 2)
        self.assertEqual(
            [line["activity"]["event_id"] for line in events[:2]],
            [line["activity"]["event_id"] for line in events[2:]],
        )
        for line in events:
            self.assertEqual(
                (line["principal_id"], line["principal_class"], line["team_id"]), ("team-local", "machine", "team_1")
            )
            self.assertEqual(set(line["activity"]), {"event_id", "phase", "subject", "incarnation", "initiator_id"})
            self.assertEqual((line["activity"]["initiator_id"], line["activity"]["incarnation"]), (AUTHOR, "c" * 64))
        journal = self.audit_path.read_text(encoding="utf-8")
        for content in ("language", "format", LANGUAGE["preference"], FORMAT["preference"], SOURCE.nonce, RESUMER):
            self.assertNotIn(content, journal)

    def test_an_eviction_at_the_bound_is_recorded_with_its_memory(self):
        self.remember(*({"op": "remember", "topic": f"t{index}", "preference": "x"} for index in range(32)))
        oldest = self.store._memory("team_1")[0]["memory_id"]
        with self.service._exclusive_chat_turn("team_1") as token:
            self._complete(token, (REMEMBER_LANGUAGE,))
        self.assertEqual(
            self.phases(),
            [
                ("memory-remember", "requested", "ok"),
                ("memory-evicted", "requested", "ok"),
                ("memory-remember", "completed", "ok"),
                ("memory-evicted", "completed", "ok"),
            ],
        )
        self.assertEqual(self.activity()[1]["activity"]["subject"], oldest)
        self.assertEqual(self.activity()[1]["activity"]["initiator_id"], AUTHOR)
        self.assertNotIn(oldest, [entry["memory_id"] for entry in self.store._memory("team_1")])

    def test_a_turn_that_consumed_selected_files_or_no_person_sent_learns_nothing(self):
        for file_ids, source in ((("a" * 32,), SOURCE), ((), None)):
            with self.subTest(source=source), self.service._exclusive_chat_turn("team_1") as token:
                reply = self._complete(token, (REMEMBER_LANGUAGE,), ZONE_THEN_RECORD, file_ids=file_ids, source=source)
                self.assertEqual(reply["reply"], "Ok.")
        self.assertEqual(self.store.load_knowledge("team_1"), [])
        self.assertEqual(self.activity(), [])

    def test_memory_is_saved_exactly_when_the_reply_commits(self):
        with self.service._exclusive_chat_turn("team_1") as token:
            self.assertEqual(self._complete(token, (REMEMBER_LANGUAGE,), ZONE_THEN_RECORD)["reply"], "Ok.")
        self.assertEqual(self.store.load_knowledge("team_1"), [LANGUAGE])
        with self.service._exclusive_chat_turn("team_1") as token:
            self._complete(token, (), ZONE_THEN_RECORD)
        self.assertEqual(self.store.load_knowledge("team_1"), [LANGUAGE])

    def test_forgetting_what_is_not_remembered_changes_and_records_nothing(self):
        with self.service._exclusive_chat_turn("team_1") as token:
            self._complete(token, ({"op": "forget", "topic": "format", "preference": ""},))
        self.assertEqual(self.activity(), [])
        self.assertEqual(list(self.store.root.glob("*.knowledge.json")), [])

    def test_a_stopped_turn_changes_nothing(self):
        with self.service._exclusive_chat_turn("team_1") as token:
            with self.service._active_chat_guard:
                self.service._cancelled_chat_tokens.add(token)
            with self.assertRaises(local_app.ApiProblem) as caught:
                self._complete(token, (REMEMBER_LANGUAGE,), ZONE_THEN_RECORD)
        self.assertEqual(caught.exception.code, "chat-stopped")
        self.assertEqual(self.store.load_knowledge("team_1"), [])
        self.assertEqual(self.activity(), [])

    def test_a_journal_that_cannot_record_the_request_fails_the_turn_before_memory_changes(self):
        self.remember({"op": "remember", **FORMAT})
        for failing in ("record_activity", "flush"):
            with (
                self.subTest(failing=failing),
                mock.patch.object(local_audit, failing, side_effect=RuntimeError("audit down")),
                self.service._exclusive_chat_turn("team_1") as token,
            ):
                with self.assertRaisesRegex(RuntimeError, "audit down"):
                    self._complete(token, (REMEMBER_LANGUAGE,), ZONE_THEN_RECORD)
                self.assertEqual(self.service._active_chat_tokens.get("team_1"), token)
            self.assertEqual(self.store.load_knowledge("team_1"), [FORMAT])

    def test_a_failed_save_records_failed_for_each_change_and_fails_the_turn(self):
        self.remember({"op": "remember", **FORMAT})
        forget = {"op": "forget", "topic": "format", "preference": ""}
        with (
            mock.patch.object(self.store, "save_knowledge", side_effect=inference_config.InferenceConfigError("disk")),
            self.service._exclusive_chat_turn("team_1") as token,
        ):
            with self.assertRaises(local_app.ApiProblem) as caught:
                self._complete(token, (REMEMBER_LANGUAGE, forget))
            self.assertEqual(self.service._active_chat_tokens.get("team_1"), token)
        self.assertEqual(caught.exception.code, "memory-store-failed")
        self.assertEqual(
            self.phases(),
            [
                ("memory-remember", "requested", "ok"),
                ("memory-forget", "requested", "ok"),
                ("memory-remember", "failed", "error"),
                ("memory-forget", "failed", "error"),
            ],
        )
        self.assertEqual({line["activity"].get("outcome") for line in self.activity()[2:]}, {"memory-store-failed"})
        self.assertEqual(self.store.load_knowledge("team_1"), [FORMAT])

    def test_a_failed_save_whose_failure_cannot_be_recorded_still_fails_the_turn_and_logs_once(self):
        real = local_audit.record_activity

        def record(operation, **fields):
            if fields["activity"]["phase"] == "failed":
                raise RuntimeError("audit down")
            return real(operation, **fields)

        with (
            mock.patch.object(local_audit, "record_activity", side_effect=record),
            mock.patch.object(self.store, "save_knowledge", side_effect=inference_config.InferenceConfigError("disk")),
            self.assertLogs("shimpz.team.local.chat.memory", level="ERROR") as logs,
            self.service._exclusive_chat_turn("team_1") as token,
            self.assertRaises(local_app.ApiProblem) as caught,
        ):
            self._complete(token, (REMEMBER_LANGUAGE,))
        self.assertEqual(caught.exception.code, "memory-store-failed")
        [line] = logs.output
        self.assertIn("failed for Team team_1", line)
        self.assertEqual(self.store.load_knowledge("team_1"), [])

    def test_a_committed_save_whose_completion_cannot_be_recorded_still_answers_with_the_change(self):
        real = local_audit.record_activity

        def record(operation, **fields):
            if fields["activity"]["phase"] == "completed":
                raise RuntimeError("audit down")
            return real(operation, **fields)

        forget = {"op": "forget", "topic": "format", "preference": ""}
        self.remember({"op": "remember", **FORMAT})
        with (
            mock.patch.object(local_audit, "record_activity", side_effect=record),
            self.assertLogs("shimpz.team.local.chat.memory", level="ERROR") as logs,
            self.service._exclusive_chat_turn("team_1") as token,
        ):
            self.assertEqual(self._complete(token, (REMEMBER_LANGUAGE, forget))["reply"], "Ok.")
            self.assertIsNone(self.service._active_chat_tokens.get("team_1"))
        self.assertEqual(self.store.load_knowledge("team_1"), [LANGUAGE])
        [line] = logs.output
        event_ids = ",".join(event["activity"]["event_id"] for event in self.activity())
        self.assertIn(f"completed for Team team_1: events {event_ids} (RuntimeError)", line)
        for content in ("language", LANGUAGE["preference"], SOURCE.nonce, AUTHOR):
            self.assertNotIn(content, line)

    def test_an_unreadable_memory_fails_the_turn_without_any_event(self):
        with (
            mock.patch.object(self.store, "_memory", side_effect=inference_config.InferenceConfigError("disk")),
            self.service._exclusive_chat_turn("team_1") as token,
            self.assertRaises(local_app.ApiProblem) as caught,
        ):
            self._complete(token, (REMEMBER_LANGUAGE,))
        self.assertEqual(caught.exception.code, local_chat_memory.STORE_FAILED)
        self.assertEqual(self.activity(), [])


class SourceTests(unittest.TestCase):
    def test_a_new_turn_captures_its_verified_sender_and_incarnation(self):
        controller = SimpleNamespace(
            assistant_lifecycle=SimpleNamespace(_network=lambda _team_id: SimpleNamespace(id="c" * 64))
        )
        identity = {"issued_at": 1, "nonce": "b" * 32}
        with local_audit.bind_request_principal(local_audit.AuditPrincipal(AUTHOR, "human")):
            self.assertEqual(local_chat_api._source(controller, "team_1", identity), SOURCE)
        with local_audit.bind_request_principal(local_audit.AuditPrincipal("admin", "machine")):
            self.assertIsNone(local_chat_api._source(controller, "team_1", identity))

    def test_a_turn_resumed_by_another_person_keeps_its_authors_source(self):
        responses: list[ResponseRequest] = []
        controller = SimpleNamespace(
            _run_chat_segment=lambda request: ("segment", request),
            _segment_response=lambda response: responses.append(response) or {"reply": "Ok."},
        )
        pending = SimpleNamespace(
            file_ids=(),
            assistant_ids=(),
            continuation=None,
            identity=("identity",),
            locale=None,
            recording=None,
            model=None,
            usage=None,
            source=SOURCE,
        )
        effective = SimpleNamespace(transcripts=(), requests_used=0)
        with local_audit.bind_request_principal(local_audit.AuditPrincipal(RESUMER, "human")):
            local_chat_pause.continue_paused(controller, "team_1", "token", pending, effective, ("openai", "key"), None)
        [response] = responses
        self.assertEqual(response.source, SOURCE)


if __name__ == "__main__":
    unittest.main()
