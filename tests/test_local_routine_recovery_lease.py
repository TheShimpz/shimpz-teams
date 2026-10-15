"""A held run's recovery runs under a registered, cancellable lease that Stop and deletion reach (ADR-0092)."""

import dataclasses
import tempfile
import threading
from unittest import mock

import routine_fixture
from test_local_routine_automatic import AutomaticCase, captured_timer
from test_local_routine_recovery import RECORD, Assistant, failed

from local import app as local_app
from local import audit as local_audit
from local.routine import card as routine_card
from local.routine import recovery as routine_recovery
from local.routine import run as routine_run
from local.routine import watchdog as routine_watchdog
from routine import claim as routine_claim
from routine import hold as routine_hold
from routine import record

PERSON = local_audit.AuditPrincipal("a" * 32, "human")


def held_incident(service) -> str:
    (incident,) = record.incidents(service.routine_store.load("team_1"))
    return incident.run_id


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

    def assert_still_held(self, service, run_id: str, assistant: Assistant) -> None:
        state = self.state(service)
        # Nothing continued: the operation ran once, and the incident and its evidence stay for the person.
        self.assertEqual([action for action, _id in assistant.calls].count("create-record"), 1)
        self.assertEqual(([item.run_id for item in record.incidents(state)], record.live_runs(state)), ([run_id], ()))
        self.assertEqual(record.incidents(state)[0].status, "unresolved")
        self.assertIsNotNone(service.routine_store.incident("team_1", run_id))

    def interrupting(self, interrupt):
        """Run ``interrupt`` once the episode's verification proved the operation absent, before anything continues."""
        real_verify = routine_recovery.verify

        def verify(service, team_id, incident_id, token, protected=None):
            verdict = real_verify(service, team_id, incident_id, token, protected)
            interrupt(service)
            return verdict

        return mock.patch.object(routine_recovery, "verify", side_effect=verify)

    def test_deletion_reaches_the_automatic_episode_and_nothing_continues(self) -> None:
        def delete(service) -> None:
            service.delete_routine("team_1", service.routine_store.load("team_1").routines[0].routine_id)

        assistant = Assistant([failed(), RECORD], [{"outcome": "not_occurred"}])
        with tempfile.TemporaryDirectory() as directory, self.capturing([]), self.interrupting(delete):
            service, _value, run_id = self.run_held(directory, assistant)
            routine_watchdog.check(service)
            state = self.state(service)
        # The deletion set the held run aside; the operation ran once and never again.
        self.assertEqual([action for action, _id in assistant.calls].count("create-record"), 1)
        self.assertEqual((state.routines, record.incidents(state)), ((), ()))
        notice = next(item for item in state.notices if item.notice_id == run_id)
        self.assertEqual((notice.outcome, notice.detail["choice"]), ("user-skipped", "delete"))

    def test_stop_reaches_the_automatic_episode_and_fences_its_continuation(self) -> None:
        def stop(service) -> None:
            routine_run.stop_routine_run(service, "team_1", held_incident(service))

        assistant = Assistant([failed(), RECORD], [{"outcome": "not_occurred"}])
        with tempfile.TemporaryDirectory() as directory, self.capturing([]), self.interrupting(stop):
            service, value, run_id = self.run_held(directory, assistant)
            self.assert_still_held(service, run_id, assistant)
            state = self.state(service)
        # The absence was proven, but the Stop came first: nothing is published, and the run is held for the card.
        self.assertEqual(self.status, "held")
        self.assertFalse(record.routine(state, value.routine_id).paused)
        self.assertEqual(state.notices[-1].outcome, "held")

    def test_a_cancelled_recovery_never_reopens_its_run(self) -> None:
        assistant = Assistant([failed()], [{"outcome": "not_occurred"}])
        with tempfile.TemporaryDirectory() as directory:
            service, _brain, value, run_id = self.held(directory, assistant)
            self.assertEqual(self.verify(service, value, run_id), "absent")
            with service._exclusive_chat_turn("team_1", value.routine_id) as token:
                with service._active_chat_guard:
                    service._cancelled_chat_tokens.add(token)
                resumed = routine_recovery.continue_run(service, "team_1", run_id, token, seconds=60)
            self.assert_still_held(service, run_id, assistant)
        self.assertEqual(resumed, "held")


class AtomicCardTests(AutomaticCase):
    def card(self, service, run_id: str) -> dict[str, object]:
        with local_audit.bind_request_principal(PERSON):
            return service.open_routine_card("team_1", run_id)

    def answer(self, service, run_id: str, card: dict[str, object], choice: str) -> dict[str, object]:
        with local_audit.bind_request_principal(PERSON):
            return service.answer_routine_card("team_1", run_id, {"tag": card["tag"], "choice": choice})

    def test_rodar_applies_only_to_the_exact_state_the_card_was_opened_on(self) -> None:
        for choice in ("run",):
            for change in ("generation", "revision"):
                assistant = Assistant([failed()], [])
                with tempfile.TemporaryDirectory() as directory, self.subTest(choice=choice, change=change):
                    service, _brain, value, run_id = self.held(directory, assistant)
                    card = self.card(service, run_id)
                    opened = routine_card._tagged(service, "team_1", run_id)
                    if change == "generation":
                        # A continuation and a new hold since the card opened leave the same incident id.
                        held = routine_hold.incident(self.state(service), run_id)
                        moved = routine_claim.generation_for(
                            routine_claim.network_of(held.generation, run_id), run_id, "s1"
                        )
                        service.routine_store.update(
                            "team_1",
                            lambda state, held=held, moved=moved: (
                                record.replace_run(state, dataclasses.replace(held, generation=moved)),
                                None,
                            ),
                        )
                    else:
                        routine_fixture.update_routine(service, value.routine_id, revision=2)
                    # Even a check that let it through is refused by the transition's own write.
                    with (
                        mock.patch.object(routine_card, "_tagged", return_value=opened),
                        self.assertRaises(local_app.ApiProblem) as caught,
                    ):
                        self.answer(service, run_id, card, choice)
                    state = self.state(service)
                    self.assertEqual(caught.exception.code, "routine-card-stale")
                    self.assertEqual(record.incidents(state)[0].status, "unresolved")
                    self.assertFalse(record.routine(state, value.routine_id).paused)

    def test_an_answer_waits_for_the_execution_slot_and_its_tag_still_answers(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            service, _brain, value, run_id = self.held(directory, Assistant([failed()], []))
            card = self.card(service, run_id)
            with service._exclusive_chat_turn("team_1"), self.assertRaises(local_app.ApiProblem) as busy:
                self.answer(service, run_id, card, "run")
            answered = self.answer(service, run_id, card, "run")
        self.assertEqual(busy.exception.code, "chat-active")
        self.assertEqual(answered["status"], "requested")
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
    def test_a_stop_before_the_episode_opens_opens_and_publishes_nothing_even_with_no_time_left(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            service, _brain, _value, run_id = self.held(directory, Assistant([failed()], []))
            service.routine_store.update(
                "team_1",
                lambda state: (
                    record.replace_run(
                        state, dataclasses.replace(routine_hold.incident(state, run_id), active_seconds_left=0)
                    ),
                    None,
                ),
            )
            with service._exclusive_chat_turn("team_1") as token:
                with service._active_chat_guard:
                    service._cancelled_chat_tokens.add(token)
                run = mock.Mock(team_id="team_1", run_id=run_id, provider="openai", token=token)
                outcome = routine_recovery.automatic(service, run)
            state = self.state(service)
            cursor = routine_recovery.routine_incident.open_recovery(service, "team_1", run_id).cursor
        self.assertEqual(outcome, "held")
        self.assertEqual(state.notices[-1].outcome, "held")
        self.assertFalse(cursor.episode)

    def test_a_deadline_after_a_persons_stop_never_turns_it_into_exhaustion(self) -> None:
        fired: list[object] = []

        box: list[object] = []

        class Interrupted(Assistant):
            def __call__(self, team, assistant, action, payload, evidence):
                if action == "find-record":
                    # The person stops the verification; its deadline passes right after.
                    routine_run.stop_routine_run(box[0], "team_1", box[1])
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
                mock.patch.object(routine_recovery.threading, "Timer", captured_timer(fired)),
                mock.patch.object(routine_recovery, "_clock", side_effect=lambda: next(ticks, 999.0)),
            ):
                run = mock.Mock(team_id="team_1", run_id=run_id, provider="openai", token=token)
                outcome = routine_recovery.automatic(service, run)
            state = self.state(service)
        self.assertEqual(outcome, "held")
        # Neither the later deadline nor the clock running past it outranks the person's earlier Stop.
        self.assertEqual(state.notices[-1].outcome, "held")
        self.assertFalse(record.routine(state, value.routine_id).paused)

    def test_nothing_continues_when_the_run_still_refuses_it_or_a_person_stopped_it_meanwhile(self) -> None:
        real_episode = routine_recovery._episode

        def stopped_after(service, run, episode):
            went_on = real_episode(service, run, episode)
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
                # The failed attempt's private values, held in memory for this execution's recovery: none here.
                operation_id = routine_recovery.routine_incident.open_recovery(service, "team_1", run_id).cursor
                protected = {operation_id.operation_id: ()}
                with service._exclusive_chat_turn("team_1") as token:
                    run = mock.Mock(
                        team_id="team_1", run_id=run_id, provider="openai", token=token, protected=protected
                    )
                    outcome = routine_recovery.automatic(service, run)
                state = self.state(service)
                self.assertEqual(outcome, "held")
                self.assertEqual([item.run_id for item in record.incidents(state)], [run_id])
                self.assertEqual(state.notices[-1].outcome, "held")
                self.assertFalse(record.routine(state, value.routine_id).paused)

    def test_a_stop_while_the_episode_opens_or_just_before_publication_publishes_nothing(self) -> None:
        def stop(service, token):
            # What a person's Stop does to the execution, under the guard.
            with service._active_chat_guard:
                service._cancelled_chat_tokens.add(token)

        real_open = routine_recovery._open
        real_unstopped = routine_run.unstopped

        def opening(service, team_id, incident_id):
            opened = real_open(service, team_id, incident_id)
            stop(service, box[-1])
            return opened

        def publishing(service, token, deadline, commit):
            stop(service, token)
            return real_unstopped(service, token, deadline, commit)

        cases = {
            "opening": (0, mock.patch.object(routine_recovery, "_open", side_effect=opening)),
            "publishing": (30, mock.patch.object(routine_run, "unstopped", side_effect=publishing)),
        }
        for name, (balance, patch) in cases.items():
            box: list[str] = []
            assistant = Assistant([failed()], [{"outcome": "not_occurred"}])
            with tempfile.TemporaryDirectory() as directory, self.subTest(name=name):
                service, _brain, value, run_id = self.held(directory, assistant)
                service.routine_store.update(
                    "team_1",
                    lambda state, run_id=run_id, balance=balance: (
                        record.replace_run(
                            state,
                            dataclasses.replace(routine_hold.incident(state, run_id), active_seconds_left=balance),
                        ),
                        None,
                    ),
                )
                with (
                    service._exclusive_chat_turn("team_1") as token,
                    patch,
                    mock.patch.object(routine_recovery, "verify", return_value="policy"),
                ):
                    box.append(token)
                    run = mock.Mock(team_id="team_1", run_id=run_id, provider="openai", token=token)
                    outcome = routine_recovery.automatic(service, run)
                state = self.state(service)
                self.assertEqual(outcome, "held")
                # The person's Stop came first: neither exhaustion nor the policy pause is published.
                self.assertEqual(state.notices[-1].outcome, "held")
                self.assertFalse(record.routine(state, value.routine_id).paused)

    def test_a_stop_after_the_decision_waits_until_the_pause_is_committed(self) -> None:
        events: list[str] = []
        started = threading.Event()
        real_pause = routine_recovery.routine_incident.pause

        def pausing(service, team_id, incident_id, reason):
            def stop() -> None:
                started.set()
                # A person's Stop arrives once publication was decided: it takes the same guard.
                with service._active_chat_guard:
                    service._cancelled_chat_tokens.add(box[0])
                events.append("stop")

            stopper = threading.Thread(target=stop)
            stopper.start()
            started.wait(5)
            stopper.join(0.2)
            real_pause(service, team_id, incident_id, reason)
            events.append("pause")
            threads.append(stopper)

        box: list[str] = []
        threads: list[threading.Thread] = []
        assistant = Assistant([failed()], [{"outcome": "not_occurred"}])
        with tempfile.TemporaryDirectory() as directory:
            service, _brain, _value, run_id = self.held(directory, assistant)
            with (
                service._exclusive_chat_turn("team_1") as token,
                mock.patch.object(routine_recovery, "verify", return_value="policy"),
                mock.patch.object(routine_recovery.routine_incident, "pause", side_effect=pausing),
            ):
                box.append(token)
                run = mock.Mock(team_id="team_1", run_id=run_id, provider="openai", token=token)
                routine_recovery.automatic(service, run)
            for thread in threads:
                thread.join(5)
            state = self.state(service)
        # The Stop could not land between the decision and the write: the pause committed first, then the Stop.
        self.assertEqual(events, ["pause", "stop"])
        self.assertEqual((state.notices[-1].outcome, state.notices[-1].detail["reason"]), ("paused", "policy"))
