"""A held run's card: its recorded failure, Rodar, and Excluir (ADR-0092 section 7, ADR-0101)."""

from __future__ import annotations

import dataclasses
import tempfile
from unittest import mock

import routine_fixture
from docker.errors import DockerException
from test_local_routine_recovery import Assistant, RecoveryCase, failed
from test_local_routine_service import ASSISTANT

from local import app as local_app
from local import audit as local_audit
from local.routine import card as routine_card
from local.routine import contracts as routine_contracts
from local.routine import incident as routine_incident
from local.routine import recovery as routine_recovery
from local.routine import run as routine_run
from local.routine import watchdog as routine_watchdog
from protocol.http.v1 import routine as http_routine
from routine import hold as routine_hold
from routine import record

PRINCIPAL = "a" * 32
DAILY = {"kind": "daily", "time": "09:00"}


class CardCase(RecoveryCase):
    @staticmethod
    def as_person(principal: str = PRINCIPAL):
        return local_audit.bind_request_principal(local_audit.AuditPrincipal(principal, "human"))

    def card(self, service, run_id: str) -> dict[str, object]:
        with self.as_person():
            card = service.open_routine_card("team_1", run_id)
        # Every card and answer Team produces is in its closed protocol view.
        self.assertEqual(http_routine.canonical_card(card), card)
        return card

    def answer(self, service, run_id: str, card, choice: str, principal: str = PRINCIPAL):
        with self.as_person(principal):
            answered = service.answer_routine_card("team_1", run_id, {"nonce": card["nonce"], "choice": choice})
        self.assertEqual(http_routine.canonical_card_answer(answered), answered)
        return answered

    def refused(self, service, run_id: str, choice: str, code: str, **changes) -> None:
        with self.assertRaises(local_app.ApiProblem) as caught:
            self.answer(service, run_id, self.card(service, run_id), choice, **changes)
        self.assertEqual(caught.exception.code, code)
        # Nothing changed: the run is still held, its Routine at the same revision, and a fresh card opens.
        state = self.state(service)
        self.assertEqual([item.status for item in state.incidents], ["unresolved"])
        self.card(service, run_id)


class CardViewTests(CardCase):
    def test_a_card_names_the_held_call_its_recorded_failure_and_exactly_two_choices(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            service, brain, value, run_id = self.held(directory, Assistant([failed()], []))
            (incident,) = service.list_routines("team_1")["incidents"]
            card = self.card(service, run_id)
        self.assertEqual(
            (card["choices"], card["assistant_id"], card["action"], card["revision"], card["position"], card["steps"]),
            (["run", "delete"], ASSISTANT, "create-record", value.revision, {"phase": "replay", "step": 2}, 2),
        )
        self.assertNotIn("recommended", card)
        failure = card["diagnostic"]["failure"]
        self.assertEqual(
            (card["evidence"], failure["error_type"], failure["http_status"], failure["provider"]),
            ("recorded", "HTTPStatusError", 404, "api.cloudflare.com"),
        )
        self.assertEqual(http_routine.canonical_incident_view(incident), incident)
        self.assertEqual(brain.calls, [])

    def test_an_unreadable_or_missing_diagnostic_is_said_never_guessed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            service, _brain, _value, run_id = self.held(directory, Assistant([failed()], []))
            with mock.patch.object(
                service.routine_diagnostics, "read", side_effect=routine_card.routine_diagnostics.DiagnosticStoreError
            ):
                unavailable = self.card(service, run_id)
            with mock.patch.object(service.routine_diagnostics, "read", return_value=()):
                absent = self.card(service, run_id)
        self.assertEqual((unavailable["evidence"], unavailable["diagnostic"]), ("unavailable", None))
        self.assertEqual((absent["evidence"], absent["diagnostic"]), ("absent", None))

    def test_a_held_run_whose_cursor_names_no_call_opens_no_card(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            service, _brain, _value, run_id = self.held(directory, Assistant([failed()], []))
            with (
                mock.patch.object(routine_incident, "held_call", return_value=routine_hold.UNKNOWN_STEP),
                self.as_person(),
                self.assertRaises(local_app.ApiProblem) as caught,
            ):
                service.open_routine_card("team_1", run_id)
            # Nothing was consumed: the card opens once the call is known.
            self.card(service, run_id)
        self.assertEqual(caught.exception.code, "routine-incident-unavailable")

    def test_an_answer_must_match_its_person_nonce_expiry_and_binding(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            service, _brain, value, run_id = self.held(directory, Assistant([failed()], []))
            cases = (
                (lambda card: self.answer(service, run_id, {**card, "nonce": "0" * 32}, "run"), "routine-card-expired"),
                (lambda card: self.answer(service, run_id, card, "run", principal="b" * 32), "routine-card-expired"),
                # Excluir and the retired choices, Recriar included, are never card answers.
                (lambda card: self.answer(service, run_id, card, "delete"), "invalid-body"),
                (lambda card: self.answer(service, run_id, card, "recreate"), "invalid-body"),
                (lambda card: self.answer(service, run_id, card, "verify"), "invalid-body"),
                (lambda card: self.answer(service, run_id, card, "skip"), "invalid-body"),
                (lambda card: self.answer(service, run_id, card, "pause"), "invalid-body"),
            )
            for attempt, code in cases:
                with self.subTest(code=code), self.assertRaises(local_app.ApiProblem) as caught:
                    attempt(self.card(service, run_id))
                self.assertEqual(caught.exception.code, code)
            # Expired after five minutes.
            clock = [1000.0]
            service.routine_cards = routine_card.CardBook(now=lambda: clock[0])
            card = self.card(service, run_id)
            clock[0] += routine_card.CARD_SECONDS
            with self.assertRaises(local_app.ApiProblem) as expired:
                self.answer(service, run_id, card, "run")
            self.assertEqual(expired.exception.code, "routine-card-expired")
            # A Routine updated since the card opened makes it stale.
            card = self.card(service, run_id)
            service.routine_store.update(
                "team_1",
                lambda state: (
                    record._replace_routine(
                        state,
                        routine_fixture.confirmed(
                            dataclasses.replace(record.routine(state, value.routine_id), revision=2)
                        ),
                    ),
                    None,
                ),
            )
            with self.assertRaises(local_app.ApiProblem) as stale:
                self.answer(service, run_id, card, "run")
            self.assertEqual(stale.exception.code, "routine-card-stale")
            with self.assertRaises(local_app.ApiProblem) as nobody:
                service.open_routine_card("team_1", run_id)
            self.assertEqual(nobody.exception.code, "routine-card-person-required")
            with self.as_person(), self.assertRaises(local_app.ApiProblem) as unknown:
                service.open_routine_card("team_1", "0" * 32)
            self.assertEqual(unknown.exception.code, "routine-incident-unavailable")


class RodarTests(CardCase):
    def test_rodar_sets_the_run_aside_and_one_fresh_run_starts_under_the_normal_claim(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            service, brain, value, run_id = self.held(directory, Assistant([failed()], []))
            # Recovery paused the Routine; Rodar is the person's decision to run it anyway.
            routine_incident.pause(service, "team_1", run_id, "exhausted")
            before = record.routine(self.state(service), value.routine_id)
            answered = self.answer(service, run_id, self.card(service, run_id), "run")
            state = self.state(service)
            requested = record.routine(state, value.routine_id)
            claim = service.claim_routine_run()
            claimed = self.state(service)
        self.assertEqual(answered["status"], "requested")
        self.assertEqual([item.status for item in state.incidents], ["released"])
        notice = next(item for item in state.notices if item.notice_id == run_id)
        self.assertEqual((notice.outcome, notice.detail["choice"]), ("user-skipped", "run"))
        self.assertEqual((requested.paused, requested.failures, requested.next_run_at), (False, 0, before.next_run_at))
        self.assertGreater(requested.run_requested, 0)
        # The fresh run is a new run with new operations; the standing cadence is unchanged.
        self.assertIsNotNone(claim)
        self.assertNotEqual(claim["run_id"], run_id)
        self.assertEqual(record.routine(claimed, value.routine_id).run_requested, 0)
        self.assertEqual(record.routine(claimed, value.routine_id).next_run_at, before.next_run_at)
        self.assertEqual(brain.calls, [])

    def test_rodar_refuses_what_could_overlap_or_drift_and_changes_nothing(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            service, _brain, value, run_id = self.held(directory, Assistant([failed()], []))
            with mock.patch.object(routine_recovery, "workload_stopped", return_value=False):
                self.refused(service, run_id, "run", "routine-workload-unquiesced")
            with mock.patch.object(routine_contracts, "current_contracts", return_value={}):
                self.refused(service, run_id, "run", "routine-contracts-changed")
            with mock.patch.object(
                routine_contracts, "current_contracts", side_effect=routine_contracts.ContractsUnavailableError
            ):
                self.refused(service, run_id, "run", "team-context-unavailable")
            busy = record.Run(
                record.new_id(),
                value.routine_id,
                "frozen",
                0,
                request_kind="human",
                assistant_id=ASSISTANT,
                action="list-zones",
                position={"phase": "replay", "step": 1},
                steps=1,
            )
            service.routine_store.update("team_1", lambda state: (dataclasses.replace(state, runs=(busy,)), None))
            self.refused(service, run_id, "run", "routine-busy")
            self.assertEqual(record.routine(self.state(service), value.routine_id).revision, value.revision)


class CorrectionTests(CardCase):
    """What the implementation audit required: no unknown workload, a kept selection, a held deletion, honest limits."""

    def test_an_attempt_whose_workload_is_unknown_or_unreadable_is_never_restarted(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            service, _brain, _value, run_id = self.held(directory, Assistant([failed()], []))
            cursor = routine_incident.open_recovery(service, "team_1", run_id).cursor
            for workload in ("", "assistant-container"):
                # An attempt Team never classified: no record of its workload, or Docker cannot say it stopped.
                service.routine_store.put_cursor("team_1", dataclasses.replace(cursor, fault="", workload=workload))
                with mock.patch.object(
                    service.assistant_lifecycle, "_assistant_container", side_effect=DockerException("down")
                ):
                    self.refused(service, run_id, "run", "routine-workload-unquiesced")

    def test_a_transition_refused_by_its_state_names_its_own_budget_code_or_the_incident(self) -> None:
        self.assertEqual(
            (
                routine_incident._transition_problem("routine-rate-limit").code,
                routine_incident._transition_problem("x").code,
            ),
            ("routine-rate-limit", "routine-incident-unavailable"),
        )

    def test_deletion_waits_for_a_stopped_writer_before_releasing_what_its_run_kept(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            service, _brain, value, run_id = self.held(directory, Assistant([failed()], []))
            service.assistant_lifecycle._fail_stop_action = mock.Mock()
            # A recovery of the held run is still unwinding when the Routine is deleted.
            routine_run.register_routine_run(service, "team_1", run_id, "unwinding", 60)
            deleting = service.delete_routine("team_1", value.routine_id)
            routine_watchdog.check(service)
            state = self.state(service)
            # Deletion is in progress: the set-aside incident and its cursor stay while the writer is registered.
            self.assertFalse(deleting["deleted"])
            self.assertEqual([item.status for item in state.incidents], ["skipped"])
            self.assertEqual(service.routine_store.cursors("team_1"), (run_id,))
            self.assertEqual([item.routine_id for item in state.routines], [value.routine_id])
            routine_run.unregister_routine_run(service, run_id)
            routine_watchdog.check(service)
            state = self.state(service)
            again = service.delete_routine("team_1", value.routine_id)
        self.assertEqual(([item.status for item in state.incidents], state.routines), (["released"], ()))
        self.assertEqual(again["deleted"], True)


class CardBookBoundTests(CardCase):
    """The book holds only cards someone could still answer: expired, settled, and deleted ones go."""

    @staticmethod
    def _card(incident: str, routine: str, expires_at: float) -> routine_card.Card:
        return routine_card.Card(PRINCIPAL, "a" * 64, incident, routine, 1, 1, "g", None, "n" * 32, expires_at)

    def test_expired_cards_are_swept_when_another_opens_or_is_answered(self) -> None:
        clock = [1000.0]
        book = routine_card.CardBook(now=lambda: clock[0])
        for index in range(1000):
            book.open("team_1", self._card(f"{index:032x}", "r" * 32, book.deadline()))
        clock[0] += routine_card.CARD_SECONDS
        fresh = self._card("f" * 32, "r" * 32, book.deadline())
        book.open("team_2", fresh)
        self.assertEqual(list(book._cards), [("team_2", fresh.incident_id)])
        clock[0] += routine_card.CARD_SECONDS
        self.assertIsNone(book.take("team_2", fresh.incident_id, fresh.nonce, PRINCIPAL))
        self.assertEqual(book._cards, {})

    def test_a_settled_incident_or_a_deleted_routine_takes_its_cards_with_it(self) -> None:
        book = routine_card.CardBook(now=lambda: 0.0)
        kept = self._card("k" * 32, "r" * 32, 10.0)
        other_team = self._card("o" * 32, "d" * 32, 10.0)
        for team_id, card in (
            ("team_1", self._card("s" * 32, "r" * 32, 10.0)),
            ("team_1", self._card("d" * 32, "d" * 32, 10.0)),
            ("team_1", kept),
            ("team_2", other_team),
        ):
            book.open(team_id, card)
        book.discard("team_1", "s" * 32)
        book.discard("team_1", "s" * 32)
        book.drop_routine("team_1", "d" * 32)
        self.assertEqual(set(book._cards), {("team_1", kept.incident_id), ("team_2", other_team.incident_id)})
        self.assertIs(book.take("team_1", kept.incident_id, kept.nonce, PRINCIPAL), kept)

    def test_deleting_the_routine_drops_its_open_card_and_releasing_the_incident_drops_it_again(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            service, _brain, value, run_id = self.held(directory, Assistant([failed()], []))
            service.assistant_lifecycle._fail_stop_action = mock.Mock()
            self.card(service, run_id)
            self.assertEqual(list(service.routine_cards._cards), [("team_1", run_id)])
            # A recovery still unwinding defers the release of the set-aside incident.
            routine_run.register_routine_run(service, "team_1", run_id, "unwinding", 60)
            service.delete_routine("team_1", value.routine_id)
            self.assertEqual(service.routine_cards._cards, {})
            # A card the book still held for the incident goes once the incident is released.
            service.routine_cards.open("team_1", self._card(run_id, "x" * 32, service.routine_cards.deadline()))
            routine_run.unregister_routine_run(service, run_id)
            routine_watchdog.check(service)
            state = self.state(service)
        self.assertEqual([item.status for item in state.incidents], ["released"])
        self.assertEqual(service.routine_cards._cards, {})
