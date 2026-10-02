"""Every way a held run's verification or continuation can fail, fails closed (ADR-0092)."""

from __future__ import annotations

import dataclasses
import tempfile
from types import SimpleNamespace
from unittest import mock

from test_local_routine_recovery import RECORD, Assistant, RecoveryCase, failed

from inference import config as inference_config
from local import app as local_app
from local.routine import card as routine_card
from local.routine import compiled as routine_compiled
from local.routine import incident as routine_incident
from local.routine import manage as routine_manage
from local.routine import recovery as routine_recovery
from local.routine import store as routine_store
from routine import cursor as routine_cursor
from routine import plan as routine_plan
from routine import record


class AssessmentEdgeTests(RecoveryCase):
    def test_drift_unavailable_evidence_and_a_finished_cursor_are_never_guessed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            service, _brain, value, run_id = self.held(directory, Assistant([failed()], []))
            with (
                mock.patch.object(service, "_team_assistants", return_value=("Team", "f" * 64, {})),
                self.assertRaises(local_app.ApiProblem) as moved,
            ):
                routine_recovery.assess(service, "team_1", run_id)
            self.assertEqual(moved.exception.code, "routine-drift")
            with (
                mock.patch.object(routine_plan, "admit", side_effect=routine_plan.PlanError("plan-pin-drift")),
                self.assertRaises(local_app.ApiProblem) as drifted,
            ):
                routine_recovery.assess(service, "team_1", run_id)
            self.assertEqual(drifted.exception.code, "routine-drift")
            with (
                mock.patch.object(service.routine_store, "incident", return_value=None),
                self.assertRaises(local_app.ApiProblem),
            ):
                routine_recovery._operation_state(service, "team_1", run_id, "x")
            finished = dataclasses.replace(self.cursor(service, run_id), step=2, operation_id=None, commitment=None)
            finished = dataclasses.replace(finished, attempts=0, fault="")
            service.routine_store.put_cursor("team_1", finished)
            self.assertIsNone(routine_recovery.assess(service, "team_1", run_id).action)
            self.assertEqual(self.verify(service, value, run_id), "none")

    def test_an_input_that_no_longer_resolves_or_binds_makes_the_verifier_unavailable(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            service, _brain, value, run_id = self.held(directory, Assistant([failed()], []))
            assessment = routine_recovery.assess(service, "team_1", run_id)
            with mock.patch.object(
                routine_plan, "resolve", side_effect=routine_plan.PlanError("plan-reference-missing")
            ):
                self.assertIsNone(routine_recovery.verifier_request(assessment))
            changed = dataclasses.replace(assessment.cursor, commitment="0" * 64)
            moved = dataclasses.replace(
                assessment, opened=routine_incident.OpenedRecovery(assessment.opened.recovery, changed)
            )
            self.assertIsNone(routine_recovery.verifier_request(moved))
            bound = {"action": "find-record", "input": {"name": {"from": "input", "pointer": "/missing"}}}
            unbindable = dataclasses.replace(assessment, action=dataclasses.replace(assessment.action, verifier=bound))
            self.assertIsNone(routine_recovery.verifier_request(unbindable))
            with mock.patch.object(routine_recovery, "verifier_request", return_value=None):
                self.assertEqual(self.verify(service, value, run_id), "unverifiable")

    def test_a_proven_absence_is_recorded_once(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            service, _brain, value, run_id = self.held(directory, Assistant([failed()], [{"outcome": "not_occurred"}]))
            self.assertEqual(self.verify(service, value, run_id), "absent")
            # Already proven: no verifier call is needed again.
            self.assertEqual(self.verify(service, value, run_id), "absent")


class VerifierCallEdgeTests(RecoveryCase):
    def test_an_unconfigured_failing_or_pausing_verifier_is_inconclusive(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            service, _brain, value, run_id = self.held(directory, Assistant([failed()], []))
            down = inference_config.InferenceConfigError("unavailable")
            with (
                mock.patch.object(service.inference_store, "load", side_effect=down),
                self.assertRaises(local_app.ApiProblem) as unconfigured,
            ):
                self.verify(service, value, run_id)
            self.assertEqual(unconfigured.exception.code, "inference-unavailable")
            refused = local_app.ApiProblem(409, "x", code="team-context-changed")
            with mock.patch.object(service, "_run_chat_segment", side_effect=refused):
                self.assertEqual(self.verify(service, value, run_id), "inconclusive")
            paused = SimpleNamespace(outcome=object())
            with mock.patch.object(service, "_run_chat_segment", return_value=paused):
                self.assertEqual(self.verify(service, value, run_id), "inconclusive")

    def test_a_verifier_runtime_asks_no_model_and_keeps_no_journal_identity(self) -> None:
        runtime = routine_recovery.VerifierRuntime(SimpleNamespace(interrupt_id="routine-verify"))
        self.assertIsNone(runtime.dispatching(None, "x"))
        self.assertIsNone(runtime.failed(None, None, None))
        self.assertIsNone(runtime.logical_operation(None))
        self.assertIsNone(runtime.purpose(None, None, "", ""))


class ContinuationEdgeTests(RecoveryCase):
    def test_a_continuation_needs_its_segment_and_a_resumable_routine(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            service, _brain, value, run_id = self.held(
                directory, Assistant([failed()], [{"outcome": "occurred", "result": RECORD}])
            )
            self.assertEqual(self.verify(service, value, run_id), "occurred")
            exhausted = dataclasses.replace(self.cursor(service, run_id), segment=routine_cursor.MAX_SEGMENTS)
            service.routine_store.put_cursor("team_1", exhausted)
            with self.assertRaises(local_app.ApiProblem) as segments:
                self.resume(service, value, run_id)
            self.assertEqual(segments.exception.code, "cursor-segments-exhausted")
            service.routine_store.put_cursor("team_1", dataclasses.replace(exhausted, segment=0))
            routine_incident.set_paused(service, "team_1", value.routine_id, True)
            with self.assertRaises(local_app.ApiProblem) as paused:
                self.resume(service, value, run_id)
            self.assertEqual(paused.exception.code, "routine-not-resumable")

    def test_reopening_refuses_a_settled_incident_a_changed_routine_or_a_busy_one(self) -> None:
        state = record.TeamRoutines()
        with self.assertRaisesRegex(record.RoutineStateError, "incident-not-found"):
            record.reopen_incident(state, "a" * 32, 0, "")
        with tempfile.TemporaryDirectory() as directory:
            service, _brain, value, run_id = self.held(directory, Assistant([failed()], []))
            state = self.state(service)
            generation = record.generation_for(self.cursor(service, run_id).binding.incarnation, run_id, "s1")
            skipped = record.skip_incident(state, run_id, 0)
            changed = record._replace_routine(
                state, dataclasses.replace(record.routine(state, value.routine_id), revision=2)
            )
            busy = dataclasses.replace(state, runs=(record.Run("b" * 32, value.routine_id, "frozen", 0),))
            for subject, target, code in (
                (skipped, generation, "incident-not-unresolved"),
                (changed, generation, "routine-not-resumable"),
                (busy, generation, "routine-busy"),
                (state, "elsewhere", "routine-busy"),
            ):
                with self.subTest(code=code), self.assertRaisesRegex(record.RoutineStateError, code):
                    record.reopen_incident(subject, run_id, 0, target)

    def test_a_continuation_hold_replaces_evidence_its_resume_left_behind(self) -> None:
        assistant = Assistant([failed(), failed()], [{"outcome": "not_occurred"}])
        with tempfile.TemporaryDirectory() as directory:
            service, _brain, value, run_id = self.held(directory, assistant)
            self.assertEqual(self.verify(service, value, run_id), "absent")
            with mock.patch.object(service.routine_store, "delete_incident"):
                self.assertEqual(self.resume(service, value, run_id), "held")
            opened = routine_incident.open_recovery(service, "team_1", run_id)
        self.assertEqual(opened.cursor.segment, 1)

    def test_a_live_runs_files_stay_while_its_old_generation_goes(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            service, _brain, _value, run_id = self.held(directory, Assistant([failed()], []))
            cursor = self.cursor(service, run_id)
            generation = record.generation_for(cursor.binding.incarnation, run_id)
            routine_manage._discard(service, "team_1", run_id, generation, incident=False, live=True)
            self.assertIsNotNone(service.routine_store.cursor("team_1", cursor.binding))

    def test_a_carried_operation_is_never_retried_without_proven_absence(self) -> None:
        seal = SimpleNamespace(
            team_id="team_1", store=SimpleNamespace(put_cursor=lambda *_args: None), diagnostics=None
        )
        binding = routine_cursor.Binding("a" * 64, "b" * 32, 1, "c" * 32)
        cursor = routine_cursor.Cursor(
            binding, "sha256:" + "d" * 64, 0, 0, "6f1c2b8e-3a4d-4c5e-9f60-718293a4b5c6", 1, "e" * 64, carried=True
        )
        runtime = routine_compiled.CompiledRuntime(seal, SimpleNamespace(digest=cursor.plan), cursor, "Name")
        with self.assertRaises(routine_compiled.CompiledRunError) as caught:
            runtime.start(None, "")
        self.assertEqual(caught.exception.code, "cursor-operation-uncertain")
        with self.assertRaisesRegex(routine_cursor.CursorError, "cursor-not-dispatched"):
            routine_cursor.proven_absent(
                dataclasses.replace(cursor, operation_id=None, attempts=0, commitment=None, carried=False)
            )
        with self.assertRaisesRegex(record.RoutineStateError, "generation-invalid"):
            record.generation_for("net", "a" * 32, "x1")

    def test_a_cursor_that_cannot_be_sealed_is_unavailable(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            service, _brain, value, run_id = self.held(directory, Assistant([failed()], [{"outcome": "not_occurred"}]))
            broken = routine_store.RoutineStoreError("down")
            with (
                mock.patch.object(service.routine_store, "put_cursor", side_effect=broken),
                self.assertRaises(local_app.ApiProblem),
            ):
                self.verify(service, value, run_id)


class CardEdgeTests(RecoveryCase):
    def test_a_book_drops_and_clears_its_cards_and_refuses_a_foreign_nonce(self) -> None:
        book = routine_card.CardBook(now=lambda: 0.0)
        card = routine_card.Card("p", "a" * 64, "i" * 32, "r" * 32, 1, 1, None, "n" * 32, 10.0)
        book.open("team_1", card)
        self.assertIsNone(book.take("team_1", card.incident_id, None, "p"))
        book.open("team_2", card)
        book.drop("team_1")
        self.assertIsNone(book.take("team_1", card.incident_id, card.nonce, "p"))
        book.clear()
        self.assertIsNone(book.take("team_2", card.incident_id, card.nonce, "p"))

    def test_a_drifted_step_is_offered_with_pausar_recommended_and_a_skipped_one_has_no_card(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            service, _brain, _value, run_id = self.held(directory, Assistant([failed()], []))
            with mock.patch.object(routine_plan, "admit", side_effect=routine_plan.PlanError("plan-pin-drift")):
                card = self.as_card(service, run_id)
            self.assertEqual((card["recommended"], card["choices"]), ("pause", ["pause", "verify", "skip"]))
            routine_incident.skip(service, "team_1", run_id)
            with self.assertRaises(local_app.ApiProblem) as caught:
                self.as_card(service, run_id)
            self.assertEqual(caught.exception.code, "routine-incident-unavailable")
            # A skipped incident can no longer pause its Routine, and a snapshot without a cursor names no step.
            with self.assertRaises(local_app.ApiProblem) as caught:
                routine_incident.pause(service, "team_1", run_id, "person")
            self.assertEqual(caught.exception.code, "routine-incident-unavailable")
            snapshot = mock.Mock()
            with mock.patch.object(service.routine_store, "cursor", return_value=None):
                self.assertEqual(routine_incident._held_step(service, "team_1", snapshot), ("", ""))

    def as_card(self, service, run_id: str) -> dict[str, object]:
        from local import audit as local_audit

        with local_audit.bind_request_principal(local_audit.AuditPrincipal("a" * 32, "human")):
            return service.open_routine_card("team_1", run_id)


class ProofEdgeTests(RecoveryCase):
    def test_a_mutating_operation_paused_before_acting_is_absent_unless_carried(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            service, _brain, _value, run_id = self.held(directory, Assistant([failed()], []))
            assessment = dataclasses.replace(routine_recovery.assess(service, "team_1", run_id), state="prepared")
            self.assertEqual(routine_recovery.proven(assessment), "absent")
            carried = dataclasses.replace(assessment.cursor, carried=True)
            moved = dataclasses.replace(
                assessment, opened=routine_incident.OpenedRecovery(assessment.opened.recovery, carried)
            )
            self.assertEqual(routine_recovery.proven(moved), "uncertain")

    def test_verificar_on_a_paused_routine_records_absence_without_continuing(self) -> None:
        from local import audit as local_audit

        with tempfile.TemporaryDirectory() as directory:
            service, _brain, value, run_id = self.held(directory, Assistant([failed()], [{"outcome": "not_occurred"}]))
            routine_incident.set_paused(service, "team_1", value.routine_id, True)
            with local_audit.bind_request_principal(local_audit.AuditPrincipal("a" * 32, "human")):
                card = service.open_routine_card("team_1", run_id)
                answered = service.answer_routine_card("team_1", run_id, {"nonce": card["nonce"], "choice": "verify"})
        self.assertEqual((answered["verdict"], answered["status"]), ("absent", None))
