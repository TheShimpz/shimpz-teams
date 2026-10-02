"""A held run's recovery runs under a registered, cancellable lease that Stop and deletion reach (ADR-0092)."""

from __future__ import annotations

import dataclasses
import tempfile
from unittest import mock

import routine_fixture
from test_local_routine_automatic import AutomaticCase, Brain
from test_local_routine_recovery import RECORD, Assistant, failed

from local import app as local_app
from local import audit as local_audit
from local.routine import card as routine_card
from local.routine import incident as routine_incident
from local.routine import recovery as routine_recovery
from routine import record

PERSON = local_audit.AuditPrincipal("a" * 32, "human")


def held_incident(service) -> str:
    (incident,) = service.routine_store.load("team_1").incidents
    return incident.incident_id


class Interrupting(Assistant):
    """The verifier call is where a person's Stop, or the Routine's deletion, arrives."""

    def __init__(self, verdicts, interrupt) -> None:
        super().__init__([failed(), RECORD], verdicts)
        self.interrupt = interrupt

    def __call__(self, team, assistant, action, payload, evidence):
        if action == "find-record":
            self.interrupt()
        return super().__call__(team, assistant, action, payload, evidence)


class RecoveryLeaseTests(AutomaticCase):
    def capturing(self, box: list[object]):
        """Keep the service the case builds, so an interruption can reach it."""
        original = self.service

        def service(*args, **kwargs):
            controller, value = original(*args, **kwargs)
            # Stop fail-stops the Action in flight; these Assistants are doubles that record it.
            controller.assistant_lifecycle._fail_stop_action = mock.Mock()
            box.append(value)
            return controller, value

        return mock.patch.object(self, "service", service)

    def answer_verify(self, service, run_id: str) -> dict[str, object]:
        with local_audit.bind_request_principal(PERSON):
            card = service.open_routine_card("team_1", run_id)
            return service.answer_routine_card("team_1", run_id, {"nonce": card["nonce"], "choice": "verify"})

    def assert_still_held(self, service, run_id: str, assistant: Assistant) -> None:
        state = self.state(service)
        # Nothing continued: the operation ran once, and the incident and its evidence stay for the person.
        self.assertEqual([action for action, _id in assistant.calls].count("create-record"), 1)
        self.assertEqual(([item.incident_id for item in state.incidents], state.runs), ([run_id], ()))
        self.assertEqual(state.incidents[0].status, "unresolved")
        self.assertIsNotNone(service.routine_store.incident("team_1", run_id))

    def test_stop_reaches_a_manual_verification_and_no_continuation_follows(self) -> None:
        box: list[object] = []
        stops: list[dict[str, object]] = []
        assistant = Interrupting(
            [{"outcome": "not_occurred"}],
            lambda: stops.append(box[0].stop_routine("team_1", held_incident(box[0]))),
        )
        with tempfile.TemporaryDirectory() as directory, self.capturing(box):
            service, _brain, _value, run_id = self.held(directory, assistant)
            answered = self.answer_verify(service, run_id)
            self.assert_still_held(service, run_id, assistant)
        self.assertEqual([item["stopped"] for item in stops], [True])
        self.assertIsNone(answered["status"])
        # The verifier Action in flight was fail-stopped.
        service.assistant_lifecycle._fail_stop_action.assert_called_once()

    def test_deletion_reaches_a_manual_verification_and_its_incident_outlives_the_routine(self) -> None:
        box: list[object] = []
        assistant = Interrupting(
            [{"outcome": "not_occurred"}],
            lambda: box[0].delete_routine("team_1", box[0].routine_store.load("team_1").routines[0].routine_id),
        )
        with tempfile.TemporaryDirectory() as directory, self.capturing(box):
            service, _brain, _value, run_id = self.held(directory, assistant)
            answered = self.answer_verify(service, run_id)
            self.assert_still_held(service, run_id, assistant)
        self.assertIsNone(answered["status"])

    def test_stop_reaches_the_automatic_episode_and_fences_its_continuation(self) -> None:
        box: list[object] = []

        class Stopping(Brain):
            def routine_recovery(self, payload, provider, model):
                box[0].stop_routine("team_1", held_incident(box[0]))
                return super().routine_recovery(payload, provider, model)

        assistant = Assistant([failed(), RECORD], [{"outcome": "not_occurred"}])
        with tempfile.TemporaryDirectory() as directory, self.capturing(box):
            service, _value, run_id = self.run_held(directory, assistant, Stopping("retry"))
            self.assert_still_held(service, run_id, assistant)
        # The Brain answered retry, but the Stop came first.
        self.assertEqual(self.status, "held")

    def test_a_stop_with_no_recovery_running_keeps_the_incident(self) -> None:
        assistant = Assistant([failed()], [])
        with tempfile.TemporaryDirectory() as directory:
            service, _brain, _value, run_id = self.held(directory, assistant)
            stopped = service.stop_routine("team_1", run_id)
            self.assert_still_held(service, run_id, assistant)
            # Once skipped, deleting the Routine has no recovery of it left to stop.
            routine_incident.skip(service, "team_1", run_id)
            routine_id = self.state(service).routines[0].routine_id
            self.assertTrue(service.delete_routine("team_1", routine_id)["deleted"])
        self.assertEqual(stopped, {"team_id": "team_1", "run_id": run_id, "stopped": False})

    def test_a_cancelled_recovery_never_reopens_its_run(self) -> None:
        assistant = Assistant([failed()], [{"outcome": "not_occurred"}])
        with tempfile.TemporaryDirectory() as directory:
            service, _brain, value, run_id = self.held(directory, assistant)
            self.assertEqual(self.verify(service, value, run_id), "absent")
            with service._exclusive_chat_turn("team_1", value.routine_id) as token:
                with service._active_chat_guard:
                    service._cancelled_chat_tokens.add(token)
                with self.assertRaises(routine_recovery.ApiProblem) as caught:
                    routine_recovery.continue_run(service, "team_1", run_id, token)
            self.assert_still_held(service, run_id, assistant)
        self.assertEqual(caught.exception.code, "routine-recovery-stopped")


class AtomicCardTests(AutomaticCase):
    def card(self, service, run_id: str) -> dict[str, object]:
        with local_audit.bind_request_principal(PERSON):
            return service.open_routine_card("team_1", run_id)

    def answer(self, service, run_id: str, card: dict[str, object], choice: str) -> dict[str, object]:
        with local_audit.bind_request_principal(PERSON):
            return service.answer_routine_card("team_1", run_id, {"nonce": card["nonce"], "choice": choice})

    def test_pular_and_pausar_apply_only_to_the_exact_state_the_card_was_opened_on(self) -> None:
        for choice in ("skip", "pause"):
            for change in ("generation", "revision"):
                assistant = Assistant([failed()], [])
                with tempfile.TemporaryDirectory() as directory, self.subTest(choice=choice, change=change):
                    service, _brain, value, run_id = self.held(directory, assistant)
                    card = self.card(service, run_id)
                    if change == "generation":
                        # A continuation and a new hold since the card opened leave the same incident id.
                        held = record.incident(self.state(service), run_id)
                        moved = record.generation_for(record.network_of(held.generation, run_id), run_id, "s1")
                        service.routine_store.update(
                            "team_1",
                            lambda state, held=held, moved=moved: (
                                record._replace_incident(state, dataclasses.replace(held, generation=moved)),
                                None,
                            ),
                        )
                    else:
                        service.routine_store.update(
                            "team_1",
                            lambda state, routine_id=value.routine_id: (
                                record._replace_routine(
                                    state,
                                    routine_fixture.granted(
                                        dataclasses.replace(record.routine(state, routine_id), revision=2)
                                    ),
                                ),
                                None,
                            ),
                        )
                    # Even a check that let it through is refused by the transition's own write.
                    with (
                        mock.patch.object(
                            routine_card,
                            "_bound",
                            side_effect=lambda _service, _team, bound: record.Expected(
                                bound.revision, bound.generation, bound.current
                            ),
                        ),
                        self.assertRaises(local_app.ApiProblem) as caught,
                    ):
                        self.answer(service, run_id, card, choice)
                    state = self.state(service)
                    self.assertEqual(caught.exception.code, "routine-card-stale")
                    self.assertEqual(state.incidents[0].status, "unresolved")
                    self.assertFalse(record.routine(state, value.routine_id).paused)

    def test_an_answer_waits_for_the_execution_slot_without_spending_its_card(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            service, _brain, value, run_id = self.held(directory, Assistant([failed()], []))
            card = self.card(service, run_id)
            with service._exclusive_chat_turn("team_1"), self.assertRaises(local_app.ApiProblem) as busy:
                self.answer(service, run_id, card, "skip")
            answered = self.answer(service, run_id, card, "skip")
        self.assertEqual(busy.exception.code, "chat-active")
        self.assertEqual(answered["status"], "skipped")
        self.assertIsNotNone(value)
