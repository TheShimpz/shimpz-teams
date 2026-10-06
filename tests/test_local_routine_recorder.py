"""The memory-only recording turn and card books, and the edges of turning a recording into a card (ADR-0101)."""

from __future__ import annotations

import dataclasses
import unittest
from types import SimpleNamespace
from unittest import mock

from test_local_routine_proposal import ASSISTANT, PRINCIPAL, _record

from inference import client as brain_runtime_client
from install import bindings
from local import app as local_app
from local import audit as local_audit
from local.routine import contracts as routine_contracts
from local.routine import proposal as routine_proposal
from local.routine import recorder as routine_recorder
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


class RecordingBookTests(unittest.TestCase):
    def test_a_recording_is_reachable_only_by_its_own_id_and_ends_with_its_turn(self) -> None:
        book = routine_recorder.RecordingBook()
        first = book.start("team_1", (PRINCIPAL, "b" * 64), "message", None, 1)
        self.assertIsNone(book.get("team_1", None))
        self.assertIsNone(book.get("team_1", "f" * 32))
        self.assertIsNone(book.get("team_2", first))
        book.protect("team_1", "f" * 32, ("other",))
        book.end("team_1", "f" * 32)
        self.assertEqual(book.get("team_1", first).protection, trace.Protection())
        second = book.start("team_1", (PRINCIPAL, "b" * 64), "again", None, 2)
        self.assertIsNone(book.get("team_1", first))
        book.end("team_2", second)
        book.end("team_1", second)
        self.assertIsNone(book.get("team_1", second))

    def test_a_trace_past_its_bound_refuses_the_recording_instead_of_cutting_it(self) -> None:
        book = routine_recorder.RecordingBook()
        recording_id = book.start("team_1", (PRINCIPAL, "b" * 64), "message", None, 1)
        large = _occurrence(trace.MAX_KEPT_BYTES - 64)
        for _attempt in range(6):
            book.occurred("team_1", recording_id, large)
        found = book.get("team_1", recording_id)
        self.assertEqual(found.refused, "routine-recording-too-large")
        self.assertLessEqual(sum(item.size for item in found.trace.occurrences), trace.MAX_TRACE_BYTES)

    def test_only_the_persons_untruncated_earlier_sends_join_the_request(self) -> None:
        conversation = (
            brain_runtime_client.RuntimeConversationEntry("user", "DNS de shimpz.com", False),
            brain_runtime_client.RuntimeConversationEntry("assistant", "zona other.org", False),
            brain_runtime_client.RuntimeConversationEntry("user", "zona blog.dev cortada", True),
            brain_runtime_client.RuntimeConversationEntry("user", "A cada hora", False),
        )
        self.assertEqual(routine_recorder.earlier_sends(conversation), ("DNS de shimpz.com", "A cada hora"))
        book = routine_recorder.RecordingBook()
        recording_id = book.start("team_1", (PRINCIPAL, "b" * 64), "Faça isso", None, 1, earlier=("DNS de shimpz.com",))
        found = book.get("team_1", recording_id)
        self.assertEqual((found.message, found.known), ("Faça isso", ("Faça isso", "DNS de shimpz.com")))

    def test_dropping_a_team_or_clearing_forgets_every_recording(self) -> None:
        book = routine_recorder.RecordingBook()
        first = book.start("team_1", (PRINCIPAL, "b" * 64), "m", None, 1)
        second = book.start("team_2", (PRINCIPAL, "b" * 64), "m", None, 1)
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
        for change in ({"op": "create"}, {"name": "a\nb"}, {"schedule": {"kind": "never"}}, {"turn_date": "2026"}):
            with self.subTest(change=change), self.assertRaises(local_app.ApiProblem) as caught:
                routine_proposal._outcome({**_record(), **change})
            self.assertEqual(caught.exception.code, "brain-runtime-failed")
        with self.assertRaises(local_app.ApiProblem):
            routine_proposal._outcome([])

    def test_a_refused_recording_answers_its_own_code(self) -> None:
        book = routine_recorder.RecordingBook()
        recording_id = book.start("team_1", (PRINCIPAL, "b" * 64), "m", None, 1)
        book._recordings["team_1"] = dataclasses.replace(book._recordings["team_1"], refused="routine-x")
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
        recorded = routine_recording.Recorded(document, {"s1": {}, "s2": {"day": "clock", "zone": "step"}}, permitted)
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
