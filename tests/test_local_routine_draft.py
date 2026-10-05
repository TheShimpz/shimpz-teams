"""A person's Routine draft: short-term memory of a Routine still being set up (ADR-0092 amendment, 2026-10-05)."""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from local_controller_harness import LocalContractCase
from test_local_routine_create import PRINCIPAL, Runtime, _body, _change

from inference import client as brain_runtime_client
from local import app as local_app
from local import audit as local_audit
from local.routine import draft as routine_draft
from local.routine import lifecycle as routine_lifecycle
from local.routine import source as routine_source
from local.routine import store as routine_store
from local.routine import watchdog as routine_watchdog
from protocol.http.v1 import payload as http_payload
from routine import request as routine_request
from routine.request import Draft, Request

FIRST = "cria uma rotina que faz isso a cada 30 segundos"
SECOND = "Uma rotina para listar minhas zonas, página 1 com 25 por página, como solicitei anteriormente"
THIRD = "A cada 30 segundos"
NEED = {
    "question": "Qual trabalho a rotina deve repetir?",
    "options": [{"label": "Listar minhas zonas", "description": ""}, {"label": "Outra coisa", "description": ""}],
    "default_index": None,
}
CAP = {
    "question": "Quantas execuções por dia, no máximo?",
    "options": [{"label": "Até 100 por dia", "description": ""}, {"label": "Até 500 por dia", "description": ""}],
    "default_index": None,
}
CONTINUOUS = [{"kind": "continuous", "gap": 30, "cap": 100}, {"kind": "continuous", "gap": 30, "cap": 500}]


def _turn(
    routine: object, clarification: dict | None = None, reply: str = "Pronto."
) -> brain_runtime_client.RuntimeTurn:
    if clarification is not None:
        reply = http_payload.render_clarification(clarification)
    return brain_runtime_client.RuntimeTurn("completed", reply, (), clarification=clarification, routine=routine)


def _need(continues: bool = False) -> dict[str, object]:
    return {"op": "need", "continues": continues}


def _cap_question(continues: bool = True, request: str = SECOND) -> dict[str, object]:
    candidate = _change(request=request, schedule=None, continues=continues)
    return {**candidate, "question": {"field": {"kind": "schedule"}, "values": CONTINUOUS, "reply": "Pronto."}}


def _composed(asked: str, question: str, answer: str) -> str:
    return f"{asked}\n\nPergunta: {question}\nResposta: {answer}"


class Scripted(Runtime):
    """A Brain whose turns follow a script of RuntimeTurns, recording what each turn saw."""

    def __init__(self, *turns: brain_runtime_client.RuntimeTurn) -> None:
        super().__init__()
        self.turns = list(turns)

    def start(self, context, _message, *, conversation=()):
        self.contexts.append(context)
        return self.turns.pop(0)


class DraftValueTests(unittest.TestCase):
    DRAFT = Draft("1" * 32, "network-1", (("said", FIRST),), (NEED["question"], "2" * 64))

    def test_a_draft_round_trips_exactly_and_anything_else_is_proven_unusable(self) -> None:
        payload = routine_draft.encode(self.DRAFT, 1_000)
        self.assertEqual(routine_draft.decode(payload), (self.DRAFT, 1_000))
        bare = Draft("3" * 32, "network-1", (("cited", "liste"), ("said", FIRST)))
        self.assertEqual(routine_draft.decode(routine_draft.encode(bare, 0)), (bare, 0))
        value = json.loads(payload)

        def changed(**changes: object) -> bytes:
            return json.dumps({**value, **changes}, sort_keys=True, separators=(",", ":")).encode()

        for broken in (
            b"\xff",
            b"[]",
            changed(version=2),
            changed(extra=1),
            changed(generation="X" * 32),
            changed(generation="1" * 31),
            changed(incarnation=""),
            changed(updated_at=-1),
            changed(updated_at="1"),
            changed(parts=[]),
            changed(parts="x"),
            changed(parts=[{"kind": "said"}]),
            changed(parts=[{"kind": "quoted", "text": "x"}]),
            changed(parts=[{"kind": "said", "text": " padded"}]),
            changed(parts=[{"kind": "said", "text": "x"}] * 9),
            changed(parts=[{"kind": "said", "text": "x" * 16_000}] * 3),
            changed(parts=[{"kind": "said", "text": "token: sk-live-0123456789abcdefghijklmnop"}]),
            changed(question={"text": "Q"}),
            changed(question={"text": "Linha\nOutra", "asked": "2" * 64}),
            changed(question={"text": "Q", "asked": "z" * 64}),
            json.dumps(value, indent=1).encode(),
        ):
            with self.subTest(payload=broken[:50]), self.assertRaises(routine_store.RoutineRecordInvalidError):
                routine_draft.decode(broken)

    def test_only_the_answer_to_exactly_the_drafts_question_is_taken(self) -> None:
        asked = hashlib.sha256(FIRST.encode()).hexdigest()
        draft = Draft("1" * 32, "network-1", (("said", FIRST),), (NEED["question"], asked))
        self.assertEqual(
            routine_draft.answer(draft, _composed(FIRST, NEED["question"], "Listar zonas")), "Listar zonas"
        )
        # Labels in any interface language bind; the original message and the question never become words.
        self.assertEqual(routine_draft.answer(draft, f"{FIRST}\n\nFrage: {NEED['question']}\nAntwort: Zonen"), "Zonen")
        for message in (
            _composed("outra mensagem", NEED["question"], "Listar zonas"),
            _composed(FIRST, "Outra pergunta?", "Listar zonas"),
            _composed(FIRST, NEED["question"], " Listar zonas"),
            _composed(FIRST, NEED["question"], "x" * 4_001),
            _composed(FIRST, NEED["question"], ""),
            f"{FIRST}\n\nPergunta: {NEED['question']}\nResposta Listar zonas",
            f"{FIRST}\n\n: {NEED['question']}\nResposta: Listar zonas",
            f"{FIRST}\n\nPergunta: {NEED['question']}\n: Listar zonas",
            f"{FIRST}\n\nPergunta {NEED['question']}\nResposta: Listar zonas",
            f"{FIRST}\n\nPergunta: {NEED['question']}",
            FIRST,
        ):
            with self.subTest(message=message[-30:]):
                self.assertIsNone(routine_draft.answer(draft, message))
        self.assertIsNone(routine_draft.answer(None, _composed(FIRST, NEED["question"], "x")))
        self.assertIsNone(routine_draft.answer(Draft("1" * 32, "n", draft.parts), _composed(FIRST, "Q", "x")))

    def test_a_draft_holds_only_whole_canonical_parts_that_resemble_no_credential(self) -> None:
        self.assertTrue(routine_draft.storable((("said", "x" * 16_000), ("said", "y" * 16_000))))
        for parts in (
            (),
            (("said", "x"),) * 9,
            (("said", "x" * 16_000), ("said", "y" * 16_000), ("said", "z")),
            (("said", "x" * 16_001),),
            (("said", "x\x00"),),
            (("other", "x"),),
            (("cited", "password=hunter2-Blue-horse"),),
        ):
            with self.subTest(parts=str(parts)[:40]):
                self.assertFalse(routine_draft.storable(parts))


class DraftStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        root = Path(directory.name)
        self.store = routine_store.RoutineStore(root / "state", root / "key" / "aes256.key")
        self.service = mock.Mock(routine_store=self.store)

    def put(self, principal: str = PRINCIPAL, updated_at: int | None = None) -> Draft:
        draft = Draft("1" * 32, "network-1", (("said", FIRST),))
        moment = int(time.time()) if updated_at is None else updated_at
        self.store.put_draft("team_1", principal, routine_draft.encode(draft, moment))
        return draft

    def test_a_live_draft_is_read_per_team_and_person_and_an_expired_one_is_removed(self) -> None:
        draft = self.put()
        self.assertEqual(routine_draft.current(self.service, "team_1", PRINCIPAL), draft)
        self.assertIsNone(routine_draft.current(self.service, "team_1", "b" * 32))
        self.assertIsNone(routine_draft.current(self.service, "team_2", PRINCIPAL))
        for moment in (int(time.time()) - routine_draft.DRAFT_SECONDS, int(time.time()) + 3_600):
            with self.subTest(moment=moment):
                self.put(updated_at=moment)
                self.assertIsNone(routine_draft.current(self.service, "team_1", PRINCIPAL))
                self.assertIsNone(self.store.draft("team_1", PRINCIPAL))

    def test_a_malformed_or_unauthenticated_draft_is_removed_and_an_unavailable_one_fails_closed(self) -> None:
        self.put()
        name = hashlib.sha256(PRINCIPAL.encode()).hexdigest() + ".draft"
        path = self.store._team_dir("team_1") / name
        for content in (b"{", b'{"algorithm":"AES-256-GCM","nonce":"!","ciphertext":"x"}'):
            with self.subTest(content=content):
                self.put()
                path.write_bytes(content)
                self.assertIsNone(routine_draft.current(self.service, "team_1", PRINCIPAL))
                self.assertFalse(path.exists())
        # Another Team's or person's sealed draft never authenticates here.
        self.store.put_draft("team_1", "b" * 32, routine_draft.encode(self.put(), 0))
        (self.store._team_dir("team_1") / (hashlib.sha256(b"b" * 32).hexdigest() + ".draft")).replace(path)
        self.assertIsNone(routine_draft.current(self.service, "team_1", PRINCIPAL))
        # A sealed record whose plaintext is not a draft is proven unusable too.
        self.store.put_draft("team_1", PRINCIPAL, b"{}")
        self.assertIsNone(routine_draft.current(self.service, "team_1", PRINCIPAL))
        self.put()
        self.store.key_path.unlink()
        with self.assertRaises(local_app.ApiProblem) as caught:
            routine_draft.current(self.service, "team_1", PRINCIPAL)
        self.assertEqual(caught.exception.code, "routine-state-unavailable")

    def test_every_change_proves_the_request_froze_exactly_the_live_draft(self) -> None:
        live = self.put()
        stale = Request(PRINCIPAL, FIRST, int(time.time()), "n" * 32)
        frozen = Request(PRINCIPAL, FIRST, int(time.time()), "n" * 32, draft=live)
        for request in (stale, Request(PRINCIPAL, FIRST, 0, "n", draft=Draft("9" * 32, "network-1", live.parts))):
            for change in (
                lambda request=request: routine_draft.save(
                    self.service, "team_1", request, "network-1", live.parts, "Q?"
                ),
                lambda request=request: routine_draft.discard(self.service, "team_1", request),
            ):
                with self.subTest(request=request.draft), self.assertRaises(local_app.ApiProblem) as caught:
                    change()
                self.assertEqual(caught.exception.code, "routine-request-expired")
                self.assertEqual(routine_draft.current(self.service, "team_1", PRINCIPAL), live)
        generation = routine_draft.save(self.service, "team_1", frozen, "network-1", (("said", FIRST),), "Q?")
        saved = routine_draft.current(self.service, "team_1", PRINCIPAL)
        self.assertEqual(
            (saved.generation, saved.question), (generation, ("Q?", hashlib.sha256(FIRST.encode()).hexdigest()))
        )
        self.assertNotEqual(generation, live.generation)
        # Parts that do not fit whole remove the draft instead of being cut down.
        after = Request(PRINCIPAL, FIRST, 0, "n", draft=saved)
        self.assertIsNone(routine_draft.save(self.service, "team_1", after, "network-1", (("said", "x"),) * 9, "Q?"))
        self.assertIsNone(routine_draft.current(self.service, "team_1", PRINCIPAL))

    def test_the_sweep_removes_only_expired_drafts_even_in_a_team_with_no_routine(self) -> None:
        self.put()
        self.put(principal="b" * 32)
        old = self.store._team_dir("team_1") / (hashlib.sha256(b"b" * 32).hexdigest() + ".draft")
        os.utime(old, (time.time() - 3_600, time.time() - 3_600))
        self.assertEqual(self.store.teams(), ())
        self.assertEqual(self.store.sweep_drafts(time.time() - routine_draft.DRAFT_SECONDS), 1)
        self.assertFalse(old.exists())
        self.assertIsNotNone(self.store.draft("team_1", PRINCIPAL))
        # Nothing is swept while a reset holds the store.
        os.utime(self.store._team_dir("team_1") / (hashlib.sha256(PRINCIPAL.encode()).hexdigest() + ".draft"), (0, 0))
        with self.store.exclusive():
            self.assertEqual(self.store.sweep_drafts(time.time()), 0)
        self.assertEqual(self.store.sweep_drafts(time.time()), 1)
        self.assertEqual(self.store.sweep_drafts(time.time()), 0)

    def test_the_sweep_fails_closed_on_a_draft_that_breaks_the_ownership_contract(self) -> None:
        self.put()
        directory = self.store._team_dir("team_1")
        (directory / ("c" * 64 + ".draft")).symlink_to(directory / "missing")
        with self.assertRaisesRegex(routine_store.RoutineStoreError, "ownership"):
            self.store.sweep_drafts(time.time())

    def test_the_sweep_and_removal_report_storage_failures(self) -> None:
        self.put()
        real = os.scandir

        def failing(error):
            # The state root still lists; only a Team directory fails, as a sweep reads it.
            return lambda path: real(path) if Path(path) == self.store.root else (_ for _ in ()).throw(error)

        listing = mock.patch.object(routine_store.os, "scandir", side_effect=failing(PermissionError()))
        with listing, self.assertRaisesRegex(routine_store.RoutineStoreError, "drafts could not be listed"):
            self.store.sweep_drafts(time.time())
        with mock.patch.object(routine_store.os, "scandir", side_effect=failing(FileNotFoundError())):
            self.assertEqual(self.store.sweep_drafts(time.time()), 0)
        syncing = mock.patch.object(routine_store.os, "fsync", side_effect=OSError)
        with syncing, self.assertRaisesRegex(routine_store.RoutineStoreError, "removed"):
            self.store.delete_draft("team_1", PRINCIPAL)
        self.put()
        unlinking = mock.patch.object(Path, "unlink", side_effect=PermissionError)
        with unlinking, self.assertRaisesRegex(routine_store.RoutineStoreError, "removed"):
            self.store.delete_draft("team_1", PRINCIPAL)
        for bad in (b"", "x", b"x" * (routine_store.MAX_DRAFT_BYTES + 1)):
            with self.subTest(size=len(bad)), self.assertRaisesRegex(routine_store.RoutineStoreError, "invalid"):
                self.store.put_draft("team_1", PRINCIPAL, bad)
        with self.assertRaisesRegex(routine_store.RoutineStoreError, "person"):
            self.store.draft("team_1", "")


class DraftJourneyTests(LocalContractCase):
    """The owner's journey: no piece the person already gave is asked for again (ADR-0092, 2026-10-05)."""

    def setUp(self) -> None:
        super().setUp()
        patch = mock.patch.object(local_audit, "record_request", return_value="a" * 32)
        patch.start()
        self.addCleanup(patch.stop)
        self.nonce = 0

    def service(self, directory: str, runtime: Runtime):
        return self._chat_controller(directory, runtime).chat_turn_service

    def chat(self, service, message: str, *, principal: str = PRINCIPAL, nonce: str | None = None, **changes):
        self.nonce += 1
        body = {**_body(message, nonce=nonce or f"{self.nonce:032x}"), **changes}
        with local_audit.bind_request_principal(local_audit.AuditPrincipal(principal, "human")):
            return service.chat("team_1", body, "openai", "sk-test-0123456789")

    def draft(self, service, principal: str = PRINCIPAL) -> Draft | None:
        return routine_draft.current(service, "team_1", principal)

    def test_three_messages_each_giving_one_piece_create_one_routine_from_all_of_them(self) -> None:
        created = _change(request=THIRD, schedule=CONTINUOUS[0], continues=True)
        runtime = Scripted(_turn(_need(), NEED), _turn(_cap_question()), _turn(created))
        runtime.turns[1] = _turn(_cap_question(), CAP)
        with tempfile.TemporaryDirectory() as directory:
            service = self.service(directory, runtime)
            asked = self.chat(service, FIRST)
            first = self.draft(service)
            capped = self.chat(service, SECOND)
            second = self.draft(service)
            self.chat(service, THIRD)
            (routine,) = service.routine_store.load("team_1").routines
            source = routine_source.load(service, "team_1", routine.routine_id)
            gone = self.draft(service)
        self.assertEqual((asked["clarification"], capped["clarification"]), (NEED, CAP))
        self.assertEqual(first.parts, (("said", FIRST),))
        self.assertEqual(first.question, (NEED["question"], hashlib.sha256(FIRST.encode()).hexdigest()))
        # Each later turn saw everything the person said before, once: never as an earlier send again.
        self.assertEqual([context.routine_draft for context in runtime.contexts], [(), first.parts, second.parts])
        self.assertEqual([context.routine_earlier for context in runtime.contexts], [(), (), ()])
        self.assertEqual(second.parts, (("said", FIRST), ("said", SECOND)))
        words = (("said", FIRST), ("said", SECOND), ("said", THIRD))
        self.assertEqual(source.parts, words)
        self.assertEqual(routine.grant["message"], routine_request.commitment(words))
        self.assertIsNone(gone)

    def test_the_draft_survives_a_restart_and_an_answer_after_it_continues_it(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            service = self.service(directory, Scripted(_turn(_need(), NEED)))
            self.chat(service, FIRST)
            # A new controller: the in-memory books start empty, the sealed draft does not.
            runtime = Scripted(_turn(None))
            restarted = self.service(directory, runtime)
            restarted.routine_recent._epoch -= 5
            self.chat(restarted, _composed(FIRST, NEED["question"], "Listar minhas zonas"))
        (context,) = runtime.contexts
        self.assertEqual((context.routine_draft, context.routine_answer), ((("said", FIRST),), "Listar minhas zonas"))
        self.assertIsNotNone(context.routines)

    def test_a_label_answer_after_a_restart_continues_through_the_draft(self) -> None:
        created = _change(request=SECOND, schedule=CONTINUOUS[0], continues=True)
        with tempfile.TemporaryDirectory() as directory:
            service = self.service(directory, Scripted(_turn(_cap_question(False), CAP)))
            self.chat(service, SECOND)
            runtime = Scripted(_turn(created))
            restarted = self.service(directory, runtime)
            restarted.routine_recent._epoch -= 5
            self.chat(restarted, _composed(SECOND, CAP["question"], "Até 100 por dia"))
            (routine,) = restarted.routine_store.load("team_1").routines
            source = routine_source.load(restarted, "team_1", routine.routine_id)
        self.assertEqual(source.parts, (("said", SECOND), ("said", "Até 100 por dia")))

    def test_a_discarded_draft_revokes_its_pending_question(self) -> None:
        runtime = Scripted(
            _turn(_cap_question(False), CAP), _turn({"op": "discard"}, reply="Nada foi criado."), _turn(None)
        )
        with tempfile.TemporaryDirectory() as directory:
            service = self.service(directory, runtime)
            self.chat(service, SECOND)
            response = self.chat(service, "esquece essa rotina")
            # The old card's own option no longer creates the abandoned Routine.
            self.chat(service, _composed(SECOND, CAP["question"], "Até 100 por dia"))
            state = service.routine_store.load("team_1")
            draft = self.draft(service)
        self.assertEqual(response["reply"], "Nada foi criado.")
        self.assertEqual((state.routines, draft), ((), None))
        self.assertIsNone(runtime.contexts[2].routines)

    def test_a_creation_by_any_path_revokes_an_older_card(self) -> None:
        created = _change(request=THIRD, schedule=CONTINUOUS[1], continues=True)
        runtime = Scripted(_turn(_cap_question(False), CAP), _turn(created), _turn(None))
        with tempfile.TemporaryDirectory() as directory:
            service = self.service(directory, runtime)
            self.chat(service, SECOND)
            self.chat(service, THIRD)
            self.chat(service, _composed(SECOND, CAP["question"], "Até 100 por dia"))
            (routine,) = service.routine_store.load("team_1").routines
        self.assertEqual(routine.schedule, CONTINUOUS[1])

    def test_a_retry_after_its_draft_changed_changes_nothing(self) -> None:
        runtime = Scripted(_turn(_need(), NEED), _turn(_need(True), NEED), _turn(_need(), NEED))
        with tempfile.TemporaryDirectory() as directory:
            service = self.service(directory, runtime)
            self.chat(service, FIRST, nonce="a" * 32)
            self.chat(service, SECOND)
            changed = self.draft(service)
            # The first send's retry froze no draft, but the person now has one: it is stale.
            with self.assertRaises(local_app.ApiProblem) as caught:
                self.chat(service, FIRST, nonce="a" * 32)
            after = self.draft(service)
        self.assertEqual(caught.exception.code, "routine-request-expired")
        self.assertEqual(after, changed)

    def test_a_committed_create_retried_touches_no_newer_draft(self) -> None:
        created = _change(request=SECOND, schedule=CONTINUOUS[0])
        runtime = Scripted(_turn(created), _turn(_need(), NEED), _turn(created))
        with tempfile.TemporaryDirectory() as directory:
            service = self.service(directory, runtime)
            self.chat(service, SECOND, nonce="a" * 32)
            self.chat(service, FIRST)
            newer = self.draft(service)
            self.chat(service, SECOND, nonce="a" * 32)
            state = service.routine_store.load("team_1")
            kept = self.draft(service)
        self.assertEqual((len(state.routines), len(state.receipts)), (1, 1))
        self.assertEqual(kept, newer)
        # Without a draft, a retry is just as idempotent.
        runtime = Scripted(_turn(created), _turn(created))
        with tempfile.TemporaryDirectory() as directory:
            service = self.service(directory, runtime)
            self.chat(service, SECOND, nonce="a" * 32)
            self.chat(service, SECOND, nonce="a" * 32)
            self.assertEqual(len(service.routine_store.load("team_1").routines), 1)

    def test_a_change_that_claims_a_draft_the_request_never_froze_is_refused(self) -> None:
        claims = _change(request=SECOND, schedule=CONTINUOUS[0], continues=True)
        for routine, clarification in ((claims, None), (_need(True), NEED), (_cap_question(), CAP)):
            with self.subTest(routine=routine.get("op")), tempfile.TemporaryDirectory() as directory:
                service = self.service(directory, Scripted(_turn(routine, clarification)))
                with self.assertRaises(local_app.ApiProblem) as caught:
                    self.chat(service, SECOND)
                self.assertEqual(caught.exception.code, "team-context-changed")
                self.assertIsNone(self.draft(service))

    def test_a_draft_from_another_team_incarnation_is_never_continued(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            service = self.service(directory, Scripted(_turn(_need(), NEED), _turn(_need(True), NEED)))
            self.chat(service, FIRST)
            live = self.draft(service)
            foreign = Draft(live.generation, "another-network", live.parts, live.question)
            service.routine_store.put_draft("team_1", PRINCIPAL, routine_draft.encode(foreign, int(time.time())))
            with self.assertRaises(local_app.ApiProblem) as caught:
                self.chat(service, THIRD)
        self.assertEqual(caught.exception.code, "team-context-changed")

    def test_a_routine_question_must_recommend_nothing_and_a_need_must_ask(self) -> None:
        recommended = {**CAP, "default_index": 0}
        for routine, clarification in (
            (_need(), {**NEED, "default_index": 0}),
            (_need(), None),
            ({"op": "need"}, NEED),
            ({"op": "discard", "x": 1}, None),
            ({**_cap_question(False), "continues": "no"}, CAP),
            (_cap_question(False), {**CAP, "options": CAP["options"][:1]}),
        ):
            with self.subTest(routine=routine), tempfile.TemporaryDirectory() as directory:
                turn = _turn(routine, clarification) if clarification else _turn(routine)
                service = self.service(directory, Scripted(turn))
                with self.assertRaises(local_app.ApiProblem) as caught:
                    self.chat(service, FIRST)
                self.assertEqual(caught.exception.code, "brain-runtime-failed")
        with tempfile.TemporaryDirectory() as directory:
            service = self.service(directory, Scripted(_turn(_cap_question(False), recommended)))
            with self.assertRaises(local_app.ApiProblem) as caught:
                self.chat(service, SECOND)
        self.assertEqual(caught.exception.code, "brain-runtime-failed")

    def test_a_draft_is_per_person_never_from_files_and_never_holds_a_secret(self) -> None:
        secret = "a cada 30 segundos use password=hunter2-Blue-horse"
        runtime = Scripted(_turn(_need(), NEED), _turn(None), _turn(_need(), NEED), _turn(None))
        with tempfile.TemporaryDirectory() as directory:
            service = self.service(directory, runtime)
            self.chat(service, FIRST)
            self.chat(service, THIRD, principal="b" * 32)
            self.chat(service, secret)
            stored = self.draft(service)
            with mock.patch.object(type(service), "_chat_setup", wraps=service._chat_setup):
                pass
        # Another person sees none of it; a part that resembles a credential is never kept.
        self.assertEqual(runtime.contexts[1].routine_draft, ())
        self.assertIsNone(stored)

    def test_deleting_or_resetting_the_team_removes_every_draft(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            service = self.service(directory, Scripted(_turn(_need(), NEED)))
            self.chat(service, FIRST)
            routine_lifecycle.delete_team_routines(service, "team_1")
            self.assertIsNone(self.draft(service))
            service.routine_store.put_draft(
                "team_1", PRINCIPAL, routine_draft.encode(Draft("1" * 32, "n", (("said", "x"),)), int(time.time()))
            )
            routine_lifecycle.delete_all_routines(service)
            self.assertIsNone(self.draft(service))

    def test_the_watchdog_sweeps_expired_drafts_and_audits_a_failed_sweep(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            service = self.service(directory, Scripted(_turn(_need(), NEED)))
            self.chat(service, FIRST)
            path = next(service.routine_store._team_dir("team_1").glob("*.draft"))
            os.utime(path, (0, 0))
            routine_watchdog.check(service)
            self.assertFalse(path.exists())
            with mock.patch.object(service.routine_store, "sweep_drafts", side_effect=routine_store.RoutineStoreError):
                with mock.patch.object(routine_watchdog, "_audit") as audit:
                    routine_watchdog.check(service)
                audit.assert_called_once_with("routine-watchdog", "draft-sweep-failed")
                with self.assertRaises(routine_store.RoutineStoreError):
                    routine_watchdog.check(service, startup=True)


if __name__ == "__main__":
    unittest.main()
