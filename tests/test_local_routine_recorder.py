"""The memory-only recording turn and card books, and the edges of turning a recording into a card (ADR-0101)."""

from __future__ import annotations

import dataclasses
import json
import unittest
from types import SimpleNamespace
from unittest import mock

import test_routine_recording as recording_cases
from test_local_routine_proposal import ASSISTANT, PRINCIPAL, _record

from inference import client as brain_runtime_client
from install import bindings
from local import app as local_app
from local import audit as local_audit
from local.routine import contracts as routine_contracts
from local.routine import proposal as routine_proposal
from local.routine import recorder as routine_recorder
from protocol.http.v1 import payload as http_payload
from protocol.http.v1 import routine as http_routine
from routine import record, trace
from routine import recording as routine_recording


def _occurrence(size: int = 0) -> trace.Occurrence:
    return trace.Occurrence(
        "6f1c2b8e-3a4d-4c5e-9f60-718293a4b5c6",
        ASSISTANT,
        "list-zones",
        "sha256:" + "a" * 64,
        True,
        1,
        trace.keep({}, {}, ()),
        trace.keep("x" * size, {}, ()),
    )


BINDING = (PRINCIPAL, "b" * 64)


def _started(message: str = "m", conversation: tuple = (), timezone: str | None = None) -> routine_recorder.Started:
    return routine_recorder.Started(message, conversation, timezone)


class RecordingBookTests(unittest.TestCase):
    def test_a_span_is_reachable_only_by_its_latest_send_and_a_persons_next_send_continues_it(self) -> None:
        book = routine_recorder.RecordingBook()
        first = book.start("team_1", BINDING, _started("message"), 1)
        self.assertIsNone(book.get("team_1", None))
        self.assertIsNone(book.get("team_1", "f" * 32))
        self.assertIsNone(book.get("team_2", first))
        book.protect("team_1", "f" * 32, ("other",))
        book.protect("team_1", first, ("kept",))
        second = book.start("team_1", BINDING, _started("again"), 2)
        self.assertIsNone(book.get("team_1", first))
        found = book.get("team_1", second)
        self.assertEqual(
            ([send.message for send in found.sends], found.protection.values),
            (["message", "again"], frozenset({"kept"})),
        )
        self.assertEqual((found.recording_id, found.started_at), (second, 2))
        book.finish("team_2", second)
        book.finish("team_1", first)
        self.assertIsNotNone(book.get("team_1", second))
        book.finish("team_1", second)
        self.assertIsNone(book.get("team_1", second))

    def test_another_person_another_incarnation_a_stale_or_refused_span_starts_anew(self) -> None:
        book = routine_recorder.RecordingBook()
        book.start("team_1", BINDING, _started(), 1)
        for binding, now in (
            ((("c" * 32), "b" * 64), 2),
            ((PRINCIPAL, "d" * 64), 3),
            (BINDING, 4 + routine_recorder.SPAN_SECONDS),
        ):
            with self.subTest(binding=binding, now=now):
                send = book.start("team_1", binding, _started(), now)
                self.assertEqual(len(book.get("team_1", send).sends), 1)
                book.start("team_1", binding, _started(), now)
        send = book.start("team_1", BINDING, _started(), 4 + routine_recorder.SPAN_SECONDS)
        book._spans["team_1"] = dataclasses.replace(book._spans["team_1"], refused="routine-x")
        fresh = book.start("team_1", BINDING, _started(), 5 + routine_recorder.SPAN_SECONDS)
        self.assertEqual((len(book.get("team_1", fresh).sends), book.get("team_1", fresh).refused), (1, ""))
        self.assertIsNone(book.get("team_1", send))

    def test_a_span_is_live_only_until_fifteen_minutes_after_its_latest_send(self) -> None:
        clock = [100.0]
        book = routine_recorder.RecordingBook(clock=lambda: clock[0])
        send = book.start("team_1", BINDING, _started(), 100)
        clock[0] = 100 + routine_recorder.SPAN_SECONDS
        self.assertIsNotNone(book.live("team_1", send))
        clock[0] += 1
        self.assertIsNone(book.live("team_1", send))
        self.assertIsNone(book.live("team_1", "f" * 32))
        self.assertIsNotNone(book.get("team_1", send))

    def test_the_oldest_sends_give_way_and_a_send_that_alone_outgrows_its_bound_refuses(self) -> None:
        book = routine_recorder.RecordingBook()
        large = _occurrence(trace.MAX_KEPT_BYTES - 64)
        for index in range(4):
            send = book.start("team_1", BINDING, _started(f"send {index}"), index)
            book.occurred("team_1", send, large)
            book.occurred("team_1", send, large)
        found = book.get("team_1", send)
        self.assertEqual([item.message for item in found.sends], ["send 2", "send 3"])
        self.assertEqual(found.refused, "")
        for _attempt in range(3):
            book.occurred("team_1", send, large)
        found = book.get("team_1", send)
        self.assertEqual((found.refused, len(found.sends)), ("routine-recording-too-large", 1))
        book.occurred("team_1", send, large)
        self.assertEqual(book.get("team_1", send), found)
        for index in range(routine_recorder.MAX_SENDS + 2):
            send = book.start("team_2", BINDING, _started(f"send {index}"), index)
        self.assertEqual(len(book.get("team_2", send).sends), routine_recorder.MAX_SENDS)
        text = "x" * (routine_recorder.MAX_TEXT_BYTES // 2)
        book.start("team_3", BINDING, _started(text), 1)
        send = book.start("team_3", BINDING, _started(text), 2)
        self.assertEqual(len(book.get("team_3", send).sends), 1)
        alone = book.start("team_4", BINDING, _started("y" * routine_recorder.MAX_TEXT_BYTES), 1)
        self.assertEqual(book.get("team_4", alone).refused, "routine-recording-too-large")

    def test_a_question_is_kept_with_its_frontier_and_both_count_sends_as_old_ones_go(self) -> None:
        book = routine_recorder.RecordingBook()
        book.start("team_1", BINDING, _started(), 1)
        first = book.start("team_1", BINDING, _started(), 2)
        manifest = routine_recording.Manifest(())
        question = routine_recording.Question("routine-work-split", manifest=manifest, frontier=1)
        intent = routine_recorder.Intent("DNS", (), None, None)
        book.asked("team_1", first, question, intent)
        found = book.get("team_1", first)
        asked = routine_recording.Asked(question.code, 2, None, manifest, question.wire())
        self.assertEqual((found.asked, found.frontier, found.intent), (asked, 1, intent))
        # A later question never moves the frontier back.
        book.asked("team_1", first, routine_recording.Question("routine-schedule-unstated"), intent)
        self.assertEqual(book.get("team_1", first).frontier, 1)
        for index in range(routine_recorder.MAX_SENDS):
            send = book.start("team_1", BINDING, _started(), 3 + index)
        found = book.get("team_1", send)
        self.assertEqual((found.asked.after, found.frontier), (0, 0))

    def test_only_the_persons_own_untruncated_earlier_lines_join_the_send(self) -> None:
        conversation = (
            brain_runtime_client.RuntimeConversationEntry("user", "DNS de shimpz.com", False),
            brain_runtime_client.RuntimeConversationEntry("assistant", "zona other.org", False),
            brain_runtime_client.RuntimeConversationEntry("user", "zona blog.dev cortada", True),
            brain_runtime_client.RuntimeConversationEntry(
                "user", "A cada hora\n\nPergunta: Qual?\nResposta: shimpz.com", False
            ),
        )
        self.assertEqual(routine_recorder.earlier_sends(conversation), ("DNS de shimpz.com", conversation[3].text))
        book = routine_recorder.RecordingBook()
        send = book.start("team_1", BINDING, _started("Faça isso", conversation, "Europe/Lisbon"), 1)
        (found,) = book.get("team_1", send).sends
        self.assertEqual(
            (found.person, found.window, found.timezone),
            (("Faça isso",), ("DNS de shimpz.com", "A cada hora", "shimpz.com"), "Europe/Lisbon"),
        )

    def test_only_a_composed_answer_that_binds_the_pending_question_is_recorded_without_the_brain(self) -> None:
        intent = routine_recorder.Intent("DNS", (), None, None)
        pending = routine_recording.Pending(("dns", "records"), "zone_id", (("a-zone", None), (123, None)))

        def answered(
            code: str, answer: str | None, *, composed: bool = True, kept: routine_recorder.Intent | None = intent
        ):
            book = routine_recorder.RecordingBook()
            send = book.start("team_1", BINDING, _started(), 1)
            book.asked("team_1", send, routine_recording.Question(code, pending=pending), kept)
            message = http_payload.compose_clarified("Liste", "Qual?", answer, "pt") if composed else answer
            send = book.start("team_1", BINDING, _started(message), 2)
            return routine_recorder.answered(book.get("team_1", send))

        cases = [
            ("routine-schedule-unstated", "a cada 30 segundos", intent),
            ("routine-schedule-unstated", "ainda não sei", None),
            ("routine-output-unstated", "Mostrar somente quando mudar", intent),
            ("routine-output-unstated", "ainda não sei", None),
            ("routine-interval-over-budget", "a cada 31 segundos", intent),
            ("routine-interval-over-budget", "todo dia às 9h", None),
            ("routine-binding-ambiguous", '"a-zone"', intent),
            ("routine-binding-ambiguous", "123", intent),
            ("routine-binding-ambiguous", "a-zone", None),
            ("routine-binding-ambiguous", '"123"', None),
            ("routine-binding-unsourced", "a cada hora", None),
        ]
        for code, answer, expected in cases:
            with self.subTest(code=code, answer=answer):
                self.assertEqual(answered(code, answer), expected)
        self.assertIsNone(answered("routine-schedule-unstated", "a cada hora", composed=False))
        # Only Admin's complete composition, ending in its answer, is an answer Team records itself.
        for message in (
            "Resposta: a cada hora",
            "Liste\n\nResposta: a cada hora",
            "Liste\n\nQuestion: Qual?\nResposta: a cada hora",
            "Liste\nPergunta: Qual?\nResposta: a cada hora",
            "Liste\nmais\nPergunta: Qual?\nResposta: a cada hora",
            "Liste\n\nPergunta: Qual?\nResposta: a cada hora\nAgora liste as zonas",
        ):
            with self.subTest(message=message):
                self.assertIsNone(answered("routine-schedule-unstated", message, composed=False))
        self.assertIsNone(answered("routine-schedule-unstated", "a cada hora", kept=None))
        self.assertIsNone(routine_recorder.answered(None))

    def test_routine_mode_follows_schedule_words_an_intent_or_a_pending_question(self) -> None:
        owner = http_payload.compose_clarified(
            http_payload.compose_clarified("Cria uma rotina pra mim", "O quê?", "Listar registros DNS", "pt"),
            "Com que frequência?",
            "A cada 30 segundos",
            "pt",
        )
        owner = http_payload.compose_clarified(owner, "De qual zona?", "shimpz.com", "pt")
        cases = (
            # An earlier composed answer states the schedule though the latest one does not.
            (owner, True),
            # Schedule words alone set the advisory mode; the Brain still reads what the message asks.
            ("Explique o job que roda a cada hora", True),
            ("Liste os registros DNS de shimpz.com", False),
        )
        for message, expected in cases:
            with self.subTest(message=message):
                book = routine_recorder.RecordingBook()
                send = book.start("team_1", BINDING, _started(message), 1)
                self.assertIs(routine_recorder.routine_mode(book.get("team_1", send)), expected)
        book = routine_recorder.RecordingBook()
        send = book.start("team_1", BINDING, _started("Liste"), 1)
        intent = routine_recorder.Intent("DNS", (), None, None)
        book.asked("team_1", send, routine_recording.Question("routine-binding-unsourced"), intent)
        self.assertIs(routine_recorder.routine_mode(book.get("team_1", send)), True)
        self.assertIs(routine_recorder.routine_mode(None), False)

    def test_a_rerun_question_shows_the_brain_its_frozen_work_bounded_and_redacted(self) -> None:
        twins = ("cloudflare/list-zones", {}, recording_cases.TWINS)
        records = ("cloudflare/list-dns-records", {"zone_id": recording_cases.SHIMPZ_ID}, {"result": []})
        lookup = recording_cases._send(twins, message="DNS de shimpz.com a cada hora")
        work = recording_cases._send(records)
        asked = recording_cases._record(lookup, work)
        answer = recording_cases._send(message=json.dumps(recording_cases.TWIN_ID))
        rerun = recording_cases._record(lookup, work, answer, asked=recording_cases._asked(asked, 2))
        self.assertEqual(rerun.code, "routine-work-rerun")
        span = self.span((lookup, work, answer), rerun)
        # The chosen twin is the exact value to send: the consumer alone runs again, with no lookup.
        self.assertEqual(
            routine_recorder.rerun_work(span),
            (
                {
                    "assistant": "cloudflare",
                    "action": "list-dns-records",
                    "count": 1,
                    "inputs": [
                        {
                            "member": "zone_id",
                            "kind": "value",
                            "value": json.dumps(recording_cases.TWIN_ID),
                            "chosen": True,
                            "source": None,
                        }
                    ],
                },
            ),
        )
        # A protected literal is withheld; a fresh value names the Action it came from, never the value.
        remembered = ("reports/fetch", {"day": "2026-10-05", "id": "remembered-1"}, {})
        changes = (recording_cases._post("tok"), recording_cases._post("tok"))
        zones = recording_cases.ZONES_CALL
        work = recording_cases._send(zones, records, remembered, *changes, message="DNS de shimpz.com a cada hora")
        asked = recording_cases._record(work)
        self.assertEqual(asked.code, "routine-binding-unsourced")
        span = self.span((work,), asked, protected=("tok",))
        shown = routine_recorder.rerun_work(span)
        self.assertEqual([item["action"] for item in shown], ["list-zones", "list-dns-records", "fetch", "post"])
        self.assertEqual(shown[1]["inputs"][0]["source"], {"assistant": "cloudflare", "action": "list-zones"})
        self.assertEqual(
            [(item["member"], item["kind"], item["value"], item["source"]) for item in shown[2]["inputs"]],
            [("day", "clock", None, None), ("id", "fresh", None, None)],
        )
        self.assertEqual((shown[3]["count"], shown[3]["inputs"][0]["value"]), (2, None))
        self.assertIsNone(
            routine_recorder.rerun_work(self.span((work,), routine_recording.Question("routine-work-split")))
        )
        self.assertIsNone(routine_recorder.rerun_work(None))

    def test_frozen_work_past_the_brain_forms_bounds_is_never_shown(self) -> None:
        wide = routine_recording.Slot(
            ("reports", "fetch"), True, tuple((f"m{index:03d}", "clock", None) for index in range(65))
        )
        many = routine_recording.Slot(("reports", "post"), False, (("t", "value", "x"),))
        for slots in ((wide,), (many,) * 257):
            with self.subTest(slots=len(slots)):
                question = routine_recording.Question(
                    "routine-binding-unsourced", manifest=routine_recording.Manifest(slots)
                )
                self.assertIsNone(routine_recorder.rerun_work(self.span((recording_cases._send(),), question)))
        ordinary = routine_recording.Question(
            "routine-binding-unsourced", manifest=routine_recording.Manifest((many,) * 256)
        )
        shown = routine_recorder.rerun_work(self.span((recording_cases._send(),), ordinary))
        self.assertEqual(http_routine.canonical_rerun(shown), list(shown))

    def test_an_invented_id_is_unsourced_and_no_source_is_guessed_for_it(self) -> None:
        # A live trace: the agent listed records for a made-up zone id, then listed the zones.
        invented = ("cloudflare/list-dns-records", {"zone_id": "0" * 32}, {"result": []})
        work = recording_cases._send(invented, recording_cases.ZONES_CALL, message="DNS dessa zona a cada hora")
        asked = recording_cases._record(work)
        self.assertEqual(asked.code, "routine-binding-unsourced")
        (records,) = [entry for entry in routine_recorder.rerun_work(self.span((work,), asked)) if entry["inputs"]]
        # No result ever held the made-up id, so nothing proves which Action returns it: the hint stays empty.
        self.assertEqual((records["action"], records["inputs"][0]["source"]), ("list-dns-records", None))

    def test_a_call_is_never_suggested_as_its_own_source(self) -> None:
        remembered = "f" * 32
        echo = ("cloudflare/list-dns-records", {"zone_id": remembered}, {"zone_id": remembered})
        work = recording_cases._send(echo, message="DNS a cada hora")
        asked = recording_cases._record(work)
        self.assertEqual(asked.code, "routine-binding-unsourced")
        (entry,) = routine_recorder.rerun_work(self.span((work,), asked))
        self.assertEqual(entry["inputs"][0]["source"], None)

    def test_a_protected_value_is_withheld_even_where_json_escaping_would_hide_it(self) -> None:
        secret = 'tok"en\\1'
        slot = routine_recording.Slot(
            ("reports", "post"), False, (("note", "value", {"text": f"use {secret} now"}), ("t", "value", "x"))
        )
        question = routine_recording.Question("routine-binding-unsourced", manifest=routine_recording.Manifest((slot,)))
        span = self.span((recording_cases._send(),), question, protected=(secret,))
        (entry,) = routine_recorder.rerun_work(span)
        self.assertEqual([item["value"] for item in entry["inputs"]], [None, '"x"'])
        lost = dataclasses.replace(span, protection=trace.Protection(lost=True))
        self.assertIsNone(routine_recorder.rerun_work(lost))

    @staticmethod
    def span(sends, question, protected=()) -> routine_recorder.Span:
        asked = routine_recording.Asked(
            question.code, len(sends), question.pending, question.manifest, question.wire(), question.chosen
        )
        protection = trace.Protection().grow(protected)
        ids = tuple(f"{index:032x}" for index in range(len(sends)))
        return routine_recorder.Span("team_1", PRINCIPAL, "b" * 64, ids, tuple(sends), protection, asked=asked)

    def test_dropping_a_team_or_clearing_forgets_every_span(self) -> None:
        book = routine_recorder.RecordingBook()
        first = book.start("team_1", BINDING, _started(), 1)
        second = book.start("team_2", BINDING, _started(), 1)
        book.drop("team_1")
        self.assertIsNone(book.get("team_1", first))
        book.clear()
        self.assertIsNone(book.get("team_2", second))


class ProposalBookTests(unittest.TestCase):
    @staticmethod
    def card(proposal_id: str, *, replaces: str | None = None, team: str = "team_1", person: str = PRINCIPAL):
        return routine_proposal.Proposal(
            proposal_id, team, person, "b" * 64, None, replaces, None, {}, "sha256:" + "0" * 64, 10.0
        )

    def test_cards_go_with_a_deleted_routine_a_team_or_a_reset(self) -> None:
        book = routine_proposal.ProposalBook(now=lambda: 0.0)
        book.put(self.card("a" * 32, replaces="r" * 32))
        book.put(self.card("b" * 32, person="c" * 32))
        book.put(self.card("c" * 32, team="team_2"))
        book.drop_routine("team_1", "r" * 32)
        book.drop_routine("team_2", "r" * 32)
        self.assertIsNone(book.take("team_1", "a" * 32, PRINCIPAL))
        self.assertIsNotNone(book.take("team_1", "b" * 32, "c" * 32))
        book.put(self.card("b" * 32))
        book.drop("team_1")
        self.assertIsNone(book.take("team_1", "b" * 32, PRINCIPAL))
        book.clear()
        self.assertIsNone(book.take("team_2", "c" * 32, PRINCIPAL))


class OutcomeTests(unittest.TestCase):
    def test_a_malformed_record_call_is_a_brain_contract_failure(self) -> None:
        for change in (
            {"op": "create"},
            {"name": "a\nb"},
            {"schedule": {"kind": "hourly", "every": 1}},
            {"output": {"mode": "show", "when": None}},
            {"turn_date": "2026"},
        ):
            with self.subTest(change=change), self.assertRaises(local_app.ApiProblem) as caught:
                routine_proposal._outcome({**_record(), **change})
            self.assertEqual(caught.exception.code, "brain-runtime-failed")
        with self.assertRaises(local_app.ApiProblem):
            routine_proposal._outcome([])

    def test_a_refused_recording_answers_its_own_code(self) -> None:
        book = routine_recorder.RecordingBook(clock=lambda: 1)
        recording_id = book.start("team_1", BINDING, _started(), 1)
        book._spans["team_1"] = dataclasses.replace(book._spans["team_1"], refused="routine-x")
        service = SimpleNamespace(routine_recordings=book)
        response = SimpleNamespace(team_id="team_1", recording=recording_id)
        with (
            local_audit.bind_request_principal(local_audit.AuditPrincipal(PRINCIPAL, "human")),
            self.assertRaises(routine_proposal.RefusedError) as caught,
        ):
            routine_proposal._recording(service, response)
        self.assertEqual(caught.exception.code, "routine-x")


class CurrentTeamTests(unittest.TestCase):
    def test_a_team_changed_since_the_turn_or_in_another_incarnation_refuses(self) -> None:
        recording = SimpleNamespace(incarnation="b" * 64)
        response = SimpleNamespace(
            team_id="team_1",
            file_ids=(),
            provider="openai",
            assistant_ids=(ASSISTANT,),
            segment=SimpleNamespace(identity=("seen",)),
        )
        for setup, identity in ((("name", "c" * 64, ()), ("seen",)), (("name", "b" * 64, ()), ("other",))):
            service = SimpleNamespace(
                _chat_setup=lambda *_args, setup=setup: setup, _chat_identity=lambda *_s, identity=identity: identity
            )
            with self.subTest(setup=setup), self.assertRaises(routine_proposal.RefusedError) as caught:
                routine_proposal._current(service, response, recording)
            self.assertEqual(caught.exception.code, "team-context-changed")


class CardTests(unittest.TestCase):
    def test_the_card_names_a_run_date_and_a_plain_reference_completely(self) -> None:
        pin = "sha256:" + "a" * 64
        steps = [
            {"id": "s1", "assistant": "dns", "action": "list", "pin": pin, "input": {}},
            {
                "id": "s2",
                "assistant": "dns",
                "action": "show",
                "pin": pin,
                "input": {
                    "day": {"kind": "run_clock", "format": "date"},
                    "zone": {"kind": "step_output", "step": "s1", "pointer": "/zone"},
                },
            },
        ]
        document = {
            "version": 3,
            "timezone": "UTC",
            "steps": steps,
            "output": {"mode": "none", "step": None, "when": None},
        }
        permitted = tuple(
            {"assistant": "dns", "action": action, "pin": pin, "read_only": True, "stored_inputs": []}
            for action in ("list", "show")
        )
        candidate = record.scheduled(
            record.Routine(
                "d" * 32, "Zonas", {"kind": "daily", "time": "09:00"}, "UTC", (), document, 0, 0, permitted=permitted
            ),
            1_800_000_000,
        )
        origins = {"s1": {}, "s2": {"day": "clock", "zone": "step"}}
        recorded = routine_recording.Recorded(
            document, origins, permitted, {"kind": "daily", "time": "09:00"}, "UTC", "browser"
        )
        view = routine_proposal.card("e" * 32, candidate, recorded, (None, 1_800_000_900))
        day, zone = view["steps"][1]["inputs"]
        self.assertEqual(
            day,
            {
                "member": "day",
                "origin": "clock",
                "value": None,
                "step": None,
                "pointer": None,
                "where": None,
                "item": None,
            },
        )
        self.assertEqual((zone["origin"], zone["step"], zone["pointer"], zone["where"]), ("step", 1, "/zone", None))
        self.assertEqual(len(view["next_runs"]), http_routine.MAX_NEXT_RUNS)
        self.assertEqual(http_routine.canonical_proposal(view), view)


class ContractsTests(unittest.TestCase):
    def test_an_unreadable_team_proves_nothing_about_a_routines_scope(self) -> None:
        for error in (
            local_app.ApiProblem(503, "down", code="docker-unavailable"),
            bindings.DynamicAssistantError("x"),
        ):
            service = SimpleNamespace(_team_assistants=mock.Mock(side_effect=error))
            with self.subTest(error=error), self.assertRaises(routine_contracts.ContractsUnavailableError):
                routine_contracts.current_contracts(service, "team_1", (ASSISTANT,))


if __name__ == "__main__":
    unittest.main()
