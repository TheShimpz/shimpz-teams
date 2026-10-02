"""A held run's recovery runs under a registered, cancellable lease that Stop and deletion reach (ADR-0092)."""

from __future__ import annotations

import dataclasses
import tempfile
from unittest import mock

import routine_fixture
from test_local_routine_automatic import AutomaticCase, Brain
from test_local_routine_recovery import RECORD, Assistant, failed

from inference import client as inference_client
from local import app as local_app
from local import audit as local_audit
from local.routine import card as routine_card
from local.routine import incident as routine_incident
from local.routine import recovery as routine_recovery
from local.routine import run as routine_run
from routine import hold as routine_hold
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

    def test_a_stop_while_the_provider_call_is_blocked_publishes_no_failure_or_pause(self) -> None:
        box: list[object] = []

        class Aborted(Brain):
            def routine_recovery(self, payload, provider, model):
                self.asked.append(payload)
                box[0].stop_routine("team_1", held_incident(box[0]))
                # What the client raises once Stop aborted its request.
                raise inference_client.BrainRuntimeError("Brain runtime request was stopped")

        brain = Aborted()
        assistant = Assistant([failed(), RECORD], [{"outcome": "not_occurred"}])
        with tempfile.TemporaryDirectory() as directory, self.capturing(box):
            service, value, run_id = self.run_held(directory, assistant, brain)
            self.assert_still_held(service, run_id, assistant)
            state = self.state(service)
        self.assertEqual((self.status, len(brain.asked)), ("held", 1))
        # Not unavailable, not paused: the run's notice still says it is held, for the person's card.
        self.assertFalse(record.routine(state, value.routine_id).paused)
        self.assertEqual(state.notices[-1].outcome, "held")


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
                        held = routine_hold.incident(self.state(service), run_id)
                        moved = record.generation_for(record.network_of(held.generation, run_id), run_id, "s1")
                        service.routine_store.update(
                            "team_1",
                            lambda state, held=held, moved=moved: (
                                routine_hold._replace_incident(state, dataclasses.replace(held, generation=moved)),
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
                            side_effect=lambda _service, _team, bound: routine_hold.Expected(
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


class DeadlineBindingTests(AutomaticCase):
    def test_a_late_deadline_never_stops_a_later_execution_of_the_same_incident(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            service, _brain, _value, run_id = self.held(directory, Assistant([failed()], []))
            service.assistant_lifecycle._fail_stop_action = mock.Mock()
            routine_run.register_routine_run(service, "team_1", run_id, "later-execution", 60)
            mark = mock.Mock()
            # The first execution's deadline fires late, after a new registration of the same incident.
            self.assertFalse(routine_run.expire_routine_run(service, "team_1", run_id, "first-execution", mark))
            self.assertNotIn("later-execution", service._cancelled_chat_tokens)
            self.assertFalse(routine_run.expire_routine_run(service, "team_2", run_id, "later-execution", mark))
            mark.assert_not_called()
            self.assertTrue(routine_run.expire_routine_run(service, "team_1", run_id, "later-execution", mark))
            self.assertIn("later-execution", service._cancelled_chat_tokens)
            mark.assert_called_once_with()
            # A deadline after a Stop changes nothing: the Stop stays its cause.
            self.assertFalse(routine_run.expire_routine_run(service, "team_1", run_id, "later-execution", mark))
            mark.assert_called_once_with()
            routine_run.unregister_routine_run(service, run_id)
            # Once it ended, nothing is left for a deadline to stop.
            self.assertFalse(routine_run.expire_routine_run(service, "team_1", run_id, "later-execution", mark))


class StopPrecedenceTests(AutomaticCase):
    def test_a_stop_before_the_episode_opens_reserves_and_publishes_nothing_even_with_no_time_left(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            service, _brain, _value, run_id = self.held(directory, Assistant([failed()], []))
            service.routine_store.update(
                "team_1",
                lambda state: (
                    routine_hold._replace_incident(
                        state, dataclasses.replace(routine_hold.incident(state, run_id), active_seconds_left=0)
                    ),
                    None,
                ),
            )
            with service._exclusive_chat_turn("team_1") as token:
                with service._active_chat_guard:
                    service._cancelled_chat_tokens.add(token)
                run = mock.Mock(team_id="team_1", run_id=run_id, token=token)
                outcome = routine_recovery.automatic(service, run, "k")
            state = self.state(service)
            cursor = routine_recovery.routine_incident.open_recovery(service, "team_1", run_id).cursor
        self.assertEqual(outcome, "held")
        self.assertEqual(state.notices[-1].outcome, "held")
        self.assertEqual(cursor.remaining("episodes"), 1)

    def test_a_deadline_after_a_persons_stop_never_turns_it_into_exhaustion(self) -> None:
        fired: list[object] = []

        class Captured:
            def __init__(self, _seconds, function) -> None:
                self.daemon = False
                fired.append(function)

            def start(self) -> None:
                return

            def cancel(self) -> None:
                return

        box: list[object] = []

        class Interrupted(Assistant):
            def __call__(self, team, assistant, action, payload, evidence):
                if action == "find-record":
                    # The person stops the verification; its deadline passes right after.
                    box[0].stop_routine("team_1", box[1])
                    fired[-1]()
                return super().__call__(team, assistant, action, payload, evidence)

        assistant = Interrupted([failed()], [{"outcome": "not_occurred"}])
        with tempfile.TemporaryDirectory() as directory:
            service, _brain, value, run_id = self.held(directory, assistant)
            service.assistant_lifecycle._fail_stop_action = mock.Mock()
            box.extend((service, run_id))
            ticks = iter([0.0, 0.0, 0.0])
            with (
                service._exclusive_chat_turn("team_1") as token,
                mock.patch.object(routine_recovery.threading, "Timer", Captured),
                mock.patch.object(routine_recovery, "_clock", side_effect=lambda: next(ticks, 999.0)),
            ):
                run = mock.Mock(team_id="team_1", run_id=run_id, token=token)
                outcome = routine_recovery.automatic(service, run, "k")
            state = self.state(service)
        self.assertEqual(outcome, "held")
        # Neither the later deadline nor the clock running past it outranks the person's earlier Stop.
        self.assertEqual(state.notices[-1].outcome, "held")
        self.assertFalse(record.routine(state, value.routine_id).paused)

    def test_nothing_continues_when_the_run_still_refuses_it_or_a_person_stopped_it_meanwhile(self) -> None:
        real_episode = routine_recovery._episode

        def stopped_after(service, run, api_key, reservation):
            went_on = real_episode(service, run, api_key, reservation)
            # The person's Stop lands right after the episode decided the run may go on.
            routine_run.stop_routine_run(service, run.team_id, run.run_id)
            return went_on

        for name, episode in (("refused", lambda *_args: True), ("stopped", stopped_after)):
            assistant = Assistant([failed(), RECORD], [{"outcome": "occurred", "result": RECORD}])
            with (
                tempfile.TemporaryDirectory() as directory,
                self.subTest(name=name),
                mock.patch.object(routine_recovery, "_episode", side_effect=episode),
            ):
                service, _brain, value, run_id = self.held(directory, assistant)
                with service._exclusive_chat_turn("team_1") as token:
                    run = mock.Mock(team_id="team_1", run_id=run_id, token=token)
                    outcome = routine_recovery.automatic(service, run, "k")
                state = self.state(service)
                self.assertEqual(outcome, "held")
                self.assertEqual([item.incident_id for item in state.incidents], [run_id])
                self.assertEqual(state.notices[-1].outcome, "held")
                self.assertFalse(record.routine(state, value.routine_id).paused)
