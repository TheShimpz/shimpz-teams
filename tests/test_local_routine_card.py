"""A held run's card: its recorded failure, Rodar, and Excluir (ADR-0092 section 7, ADR-0101)."""

import contextlib
import dataclasses
import tempfile
from unittest import mock

import routine_fixture
from docker.errors import DockerException
from test_local_routine_recovery import Assistant, Brain, RecoveryCase, failed
from test_local_routine_service import ASSISTANT

from local import app as local_app
from local import audit as local_audit
from local.routine import card as routine_card
from local.routine import contracts as routine_contracts
from local.routine import incident as routine_incident
from local.routine import recovery as routine_recovery
from local.routine import run as routine_run
from local.routine import store as routine_store
from local.routine import watchdog as routine_watchdog
from protocol.http.v1 import routine_notice as http_routine_notice
from protocol.http.v1 import routine_run as http_routine_run
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
        self.assertEqual(http_routine_run.canonical_card(card), card)
        return card

    def answer(self, service, run_id: str, card):
        with self.as_person():
            answered = service.answer_routine_card("team_1", run_id, {"tag": card["tag"]})
        self.assertEqual(http_routine_run.canonical_card_answer(answered), answered)
        return answered

    def refused(self, service, run_id: str, code: str) -> None:
        with self.assertRaises(local_app.ApiProblem) as caught:
            self.answer(service, run_id, self.card(service, run_id))
        self.assertEqual(caught.exception.code, code)
        # Nothing changed: the run is still held, its Routine at the same revision, and a fresh card opens.
        state = self.state(service)
        self.assertEqual([item.status for item in record.incidents(state)], ["unresolved"])
        self.card(service, run_id)


class CardViewTests(CardCase):
    def test_a_card_names_the_held_call_its_recorded_failure_and_exactly_two_choices(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            service, brain, value, run_id = self.held(directory, Assistant([failed()], []))
            (incident,) = service.list_routines("team_1")["incidents"]
            card = self.card(service, run_id)
        self.assertEqual(
            (card["assistant_id"], card["action"], card["revision"], card["position"], card["steps"]),
            (ASSISTANT, "create-record", value.revision, 2, 2),
        )
        failure = card["diagnostic"]["failure"]
        self.assertEqual(
            (card["evidence"], failure["error_type"], failure["http_status"], failure["provider"]),
            ("recorded", "HTTPStatusError", 404, "api.cloudflare.com"),
        )
        self.assertEqual(http_routine_notice.canonical_incident_view(incident), incident)
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

    def test_the_card_names_the_call_its_hold_indexed_even_once_the_cursor_moved_on(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            service, _brain, _value, run_id = self.held(directory, Assistant([failed()], []))
            indexed = routine_hold.held_step(routine_hold.incident(self.state(service), run_id))
            # A cursor sealed past the held call, as after a verified occurrence, never renames the card's call.
            with mock.patch.object(routine_incident, "held_call", return_value=("other", "other-action", 9, 9)):
                card = self.card(service, run_id)
        self.assertEqual((card["assistant_id"], card["action"], card["position"], card["steps"]), indexed)

    def test_a_cursor_unreadable_while_holding_leaves_the_run_held_until_a_pass_indexes_its_call(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            service = self.mutating(directory, Brain(), Assistant([failed()], []))
            service._recover_routine_run = lambda _run, _progress=None: "held"
            value = self.zone_record_routine(service)
            claim = service.claim_routine_run()
            held_step = routine_incident._held_step

            def unreadable(owner, team_id, snapshot):
                # The store fails transiently exactly while the hold reads the sealed cursor for its call.
                down = routine_store.RoutineStoreError("down")
                with mock.patch.object(owner.routine_store, "cursor", side_effect=down):
                    return held_step(owner, team_id, snapshot)

            with mock.patch.object(routine_incident, "_held_step", unreadable), contextlib.suppress(Exception):
                self.run_claim(service, claim)
            # The hold failed before indexing, so the run is still held and no card names an unknown call.
            self.assertEqual([item.status for item in self.state(service).runs], ["held"])
            routine_watchdog.check(service)
            (incident,) = record.incidents(self.state(service))
            card = self.card(service, claim["run_id"])
        self.assertEqual((incident.run_id, incident.status), (claim["run_id"], "unresolved"))
        self.assertEqual(
            (card["assistant_id"], card["action"], card["revision"], card["position"], card["steps"]),
            (ASSISTANT, "create-record", value.revision, 2, 2),
        )

    def test_an_answer_must_name_the_tag_of_the_state_it_finds(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            service, _brain, value, run_id = self.held(directory, Assistant([failed()], []))
            cases = (
                (lambda card: self.answer(service, run_id, {**card, "tag": "0" * 64}), "routine-card-stale"),
                (lambda card: self.answer(service, run_id, {**card, "tag": "x"}), "invalid-body"),
            )
            for attempt, code in cases:
                with self.subTest(code=code), self.assertRaises(local_app.ApiProblem) as caught:
                    attempt(self.card(service, run_id))
                self.assertEqual(caught.exception.code, code)
            # Team keeps no card: the same state always opens the same tag.
            card = self.card(service, run_id)
            self.assertEqual(self.card(service, run_id)["tag"], card["tag"])
            # A Routine updated since the card opened makes it stale.
            routine_fixture.update_routine(service, value.routine_id, confirm=True, revision=2)
            with self.assertRaises(local_app.ApiProblem) as stale:
                self.answer(service, run_id, card)
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
            card = self.card(service, run_id)
            answered = self.answer(service, run_id, card)
            # The answered run is no longer held, so the same tag never answers twice.
            with self.assertRaises(local_app.ApiProblem) as again:
                self.answer(service, run_id, card)
            state = self.state(service)
            requested = record.routine(state, value.routine_id)
            claim = service.claim_routine_run()
            claimed = self.state(service)
        self.assertEqual(answered, {"team_id": "team_1", "run_id": run_id})
        self.assertEqual(again.exception.code, "routine-incident-unavailable")
        self.assertEqual(record.incidents(state), ())
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
                self.refused(service, run_id, "routine-workload-unquiesced")
            with mock.patch.object(routine_contracts, "current_contracts", return_value={}):
                self.refused(service, run_id, "routine-contracts-changed")
            with mock.patch.object(
                routine_contracts, "current_contracts", side_effect=routine_contracts.ContractsUnavailableError
            ):
                self.refused(service, run_id, "team-context-unavailable")
            routine_fixture.put_frozen_run(service, value.routine_id, ASSISTANT, step=1, steps=1)
            self.refused(service, run_id, "routine-busy")
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
                    self.refused(service, run_id, "routine-workload-unquiesced")

    def test_a_transition_refused_by_its_state_names_its_own_budget_code_or_the_incident(self) -> None:
        self.assertEqual(
            (
                routine_incident._transition_problem("routine-step-budget").code,
                routine_incident._transition_problem("x").code,
            ),
            ("routine-step-budget", "routine-incident-unavailable"),
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
            self.assertEqual([item.status for item in record.incidents(state)], ["skipped"])
            self.assertEqual(service.routine_store.cursors("team_1"), (run_id,))
            # A run set aside is no longer held: its card no longer opens.
            with self.as_person(), self.assertRaises(local_app.ApiProblem) as settled:
                service.open_routine_card("team_1", run_id)
            self.assertEqual((settled.exception.status, settled.exception.code), (409, "routine-incident-unavailable"))
            self.assertEqual([item.routine_id for item in state.routines], [value.routine_id])
            routine_run.unregister_routine_run(service, run_id)
            routine_watchdog.check(service)
            state = self.state(service)
            again = service.delete_routine("team_1", value.routine_id)
        self.assertEqual((record.incidents(state), state.routines), ((), ()))
        self.assertEqual(again["deleted"], True)
