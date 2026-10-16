"""A held run's one automatic recovery episode: verification first, one retry on proven absence (ADR-0092)."""

import dataclasses
import tempfile
import threading
import time
from types import SimpleNamespace
from unittest import mock

import routine_fixture
from test_local_chat_scope import LOOKUP_INPUT
from test_local_routine_recovery import RECORD, Assistant, RecoveryCase, failed
from test_local_routine_replay import ZONE
from test_local_routine_service import ASSISTANT

from action import journal as action_journal
from install import bindings
from local.routine import incident as routine_incident
from local.routine import lifecycle as routine_lifecycle
from local.routine import recovery as routine_recovery
from local.routine import state as routine_state
from local.routine import store as routine_store
from routine import claim as routine_claim
from routine import hold as routine_hold
from routine import record


def captured_timer(fired: list[object], delays: list[float] | None = None) -> type:
    """The deadline timer class, which records its callback (and delay) for the test to fire where it chooses."""

    class Captured:
        def __init__(self, seconds, function) -> None:
            self.daemon = False
            fired.append(function)
            if delays is not None:
                delays.append(seconds)

        def start(self) -> None:
            return

        def cancel(self) -> None:
            return

    return Captured


class Brain:
    """The Brain, which no Routine run or recovery episode ever reaches."""

    def __getattr__(self, name: str):
        raise AssertionError(f"the episode asked the Brain to {name}")


class AutomaticCase(RecoveryCase):
    def run_held(self, directory: str, assistant: Assistant, brain: Brain | None = None):
        """Claim and run; the hold runs its automatic episode at once."""
        service = self.mutating(directory, brain or Brain(), assistant)
        # The Assistant may reach the Team's state while its Action runs, as a person changing it meanwhile would.
        assistant.service = service
        value = self.zone_record_routine(service)
        claim = service.claim_routine_run()
        self.status = self.run_claim(service, claim)["status"]
        return service, value, claim["run_id"]


class AutomaticTests(AutomaticCase):
    def test_proven_absence_retries_the_same_operation_once(self) -> None:
        assistant = Assistant([failed(), RECORD], [{"outcome": "not_occurred"}])
        with tempfile.TemporaryDirectory() as directory:
            service, _value, _run_id = self.run_held(directory, assistant)
            state = self.state(service)
        self.assertEqual((self.status, record.incidents(state), state.runs), ("recovered", (), ()))
        creates = [operation for action, operation in assistant.calls if action == "create-record"]
        self.assertEqual((len(creates), creates[0]), (2, creates[1]))

    def test_a_reopened_run_retries_when_its_resolved_evidence_cannot_be_removed_yet(self) -> None:
        assistant = Assistant([failed(), RECORD], [{"outcome": "not_occurred"}])
        failures = [routine_store.RoutineStoreError("incident cannot be removed")]
        with tempfile.TemporaryDirectory() as directory:
            service = self.mutating(directory, Brain(), assistant)
            assistant.service = service
            self.zone_record_routine(service)
            real = service.routine_store.delete_incident

            def once(*args):
                if failures:
                    raise failures.pop()
                return real(*args)

            with mock.patch.object(service.routine_store, "delete_incident", side_effect=once):
                status = self.run_claim(service, service.claim_routine_run())["status"]
            state = self.state(service)
        # The continuation already reopened the run, so the retry runs; the run's end removes the evidence.
        self.assertEqual((status, record.incidents(state), state.runs), ("recovered", (), ()))
        self.assertEqual(len([action for action, _operation in assistant.calls if action == "create-record"]), 2)

    def test_a_verifier_whose_journal_fails_is_inconclusive_and_holds_for_the_card(self) -> None:
        assistant = Assistant([failed()], [{"outcome": "occurred", "result": RECORD}])
        with tempfile.TemporaryDirectory() as directory:
            service = self.mutating(directory, Brain(), assistant)
            assistant.service = service
            self.zone_record_routine(service)
            real = service.action_state.discard
            verifications: list[str] = []

            def failing(generation):
                if generation.endswith(":" + routine_claim.VERIFY_SUFFIX) and not verifications:
                    verifications.append(generation)
                    raise action_journal.ActionJournalError("Action generation could not be discarded")
                return real(generation)

            with mock.patch.object(service.action_state, "discard", side_effect=failing):
                status = self.run_claim(service, service.claim_routine_run())["status"]
            state = self.state(service)
        self.assertEqual((status, len(verifications), state.notices[-1].outcome), ("held", 1, "held"))
        # The verifier was never dispatched, so its occurrence could not complete the step.
        self.assertEqual([action for action, _operation in assistant.calls], ["list-zones", "create-record"])

    def test_a_proven_occurrence_continues(self) -> None:
        assistant = Assistant([failed()], [{"outcome": "occurred", "result": RECORD}])
        with tempfile.TemporaryDirectory() as directory:
            service, _value, _run_id = self.run_held(directory, assistant)
            state = self.state(service)
        self.assertEqual((self.status, record.incidents(state)), ("recovered", ()))

    def test_inconclusive_evidence_holds_for_the_card(self) -> None:
        assistant = Assistant([failed()], [{"outcome": "inconclusive"}])
        with tempfile.TemporaryDirectory() as directory:
            service, value, run_id = self.run_held(directory, assistant)
            state = self.state(service)
            cursor = self.cursor(service, run_id)
        self.assertEqual(self.status, "held")
        self.assertEqual([item.run_id for item in record.incidents(state)], [run_id])
        self.assertFalse(record.routine(state, value.routine_id).paused)
        self.assertTrue(cursor.episode)
        # The held run's one notice says it is held; its incident names its step, for the person's card.
        (incident,) = record.incidents(state)
        notice = state.notices[-1]
        self.assertEqual((notice.notice_id, notice.outcome, notice.detail), (run_id, "held", {}))
        self.assertEqual(
            (incident.assistant_id, incident.action, incident.position, incident.steps),
            (ASSISTANT, "create-record", 2, 2),
        )

    def test_the_episode_runs_once_and_the_retry_never_repeats(self) -> None:
        assistant = Assistant([failed(), failed()], [{"outcome": "not_occurred"}, {"outcome": "not_occurred"}])
        with tempfile.TemporaryDirectory() as directory:
            service, _value, run_id = self.run_held(directory, assistant)
            cursor = self.cursor(service, run_id)
        self.assertEqual(self.status, "held")
        self.assertEqual([action for action, _id in assistant.calls].count("create-record"), 2)
        self.assertEqual((cursor.episode, cursor.continued), (True, True))

    def test_an_episode_past_its_active_time_stops_and_a_restart_never_starts_another(self) -> None:
        assistant = Assistant([failed()], [{"outcome": "not_occurred"}])
        ticks = iter([0.0])
        with tempfile.TemporaryDirectory() as directory:
            with mock.patch.object(routine_recovery, "_clock", side_effect=lambda: next(ticks, 61.0)):
                service, _value, run_id = self.run_held(directory, assistant)
            cursor = self.cursor(service, run_id)
            run = mock.Mock(team_id="team_1", run_id=run_id, token=run_id)
            again = routine_recovery.automatic(service, run)
            state = self.state(service)
        self.assertEqual((self.status, again), ("held", "held"))
        self.assertTrue(cursor.episode)
        # Running out of recovery time publishes the pause and its reason, not a plain hold.
        self.assertEqual((state.notices[-1].outcome, state.notices[-1].detail["reason"]), ("paused", "exhausted"))

    def test_the_episode_opens_durably_first_and_runs_under_a_cancellable_deadline(self) -> None:
        assistant = Assistant([failed()], [{"outcome": "inconclusive"}])
        observed: list[tuple[bool, float]] = []
        real_verify = routine_recovery.verify

        def watching(service, team_id, incident_id, token, protected=None):
            # While the episode works, it is already sealed open and registered to its deadline.
            cursor = routine_recovery.routine_incident.open_recovery(service, team_id, incident_id).cursor
            registration = service._routine_runs[incident_id]
            observed.append((cursor.episode, registration.deadline - time.monotonic()))
            return real_verify(service, team_id, incident_id, token, protected)

        with (
            tempfile.TemporaryDirectory() as directory,
            mock.patch.object(routine_recovery, "verify", side_effect=watching),
        ):
            service, _value, run_id = self.run_held(directory, assistant)
            registered = run_id in service._routine_runs
            again = routine_recovery._open(service, "team_1", run_id)
        ((opened, left),) = observed
        self.assertTrue(opened)
        self.assertTrue(55 < left <= 60)
        self.assertFalse(registered)
        # The episode was sealed before its work, so no later execution or restart opens another.
        self.assertIsNone(again)


class AutomaticEdgeTests(AutomaticCase):
    def test_time_running_out_after_evidence_or_a_failing_assessment_holds_the_run(self) -> None:
        assistant = Assistant([failed()], [{"outcome": "occurred", "result": RECORD}])
        # Still in time when the verifier is dispatched; out of time once it answered.
        ticks = iter([0.0, 0.0, 0.0])
        with tempfile.TemporaryDirectory() as directory:
            with mock.patch.object(routine_recovery, "_clock", side_effect=lambda: next(ticks, 61.0)):
                service, _value, run_id = self.run_held(directory, assistant)
            self.assertEqual(self.status, "held")
            self.assertEqual(self.cursor(service, run_id).step, 2)
        assistant = Assistant([failed()], [])
        drift = routine_recovery.ApiProblem(409, "x", code="routine-drift")
        with (
            tempfile.TemporaryDirectory() as directory,
            mock.patch.object(routine_recovery, "verify", side_effect=drift),
        ):
            self.run_held(directory, assistant)
        self.assertEqual(self.status, "held")
        with (
            mock.patch.object(routine_recovery.routine_incident, "open_recovery", side_effect=drift),
            mock.patch.object(routine_recovery, "_clock", return_value=1.0),
        ):
            self.assertIsNone(routine_recovery._open(None, "team_1", "a" * 32))

    def test_a_verification_generation_its_own_release_left_goes_with_its_team(self) -> None:
        # The verifier fails, then its generation's final release fails too: its ended batch is left behind.
        ordinary = Assistant([failed()], [])

        def answer(team, assistant_id, action, payload, evidence):
            if action == "find-record":
                raise failed()
            return ordinary(team, assistant_id, action, payload, evidence)

        with tempfile.TemporaryDirectory() as directory:
            service = self.mutating(directory, Brain(), answer)
            self.zone_record_routine(service)
            real = service.action_state.discard
            verifications: list[str] = []

            def leaving(generation):
                if generation.endswith(":" + routine_claim.VERIFY_SUFFIX):
                    verifications.append(generation)
                    if len(verifications) == 2:
                        raise action_journal.ActionJournalError("Action generation could not be discarded")
                return real(generation)

            with mock.patch.object(service.action_state, "discard", side_effect=leaving):
                status = self.run_claim(service, service.claim_routine_run())["status"]
            self.assertEqual((status, len(verifications)), ("held", 2))
            self.assertEqual(service.action_state.current_batch(verifications[0])[1], "ended")
            # Deleting the Team's Routines removes it with the run's own generation: nothing is left.
            routine_lifecycle._delete_team_routines(service, "team_1")
            self.assertIsNone(service.action_state.current_batch(verifications[0]))

    def test_a_verified_occurrence_that_loses_protection_completes_and_marks_the_run(self) -> None:
        # The recovered result holds more secret strings than one run may protect: protection is first lost here.
        many = SimpleNamespace(secret_values=lambda *_args: [f"value-{index}" for index in range(200)])
        assistant = Assistant([failed()], [{"outcome": "occurred", "result": RECORD}])
        with tempfile.TemporaryDirectory() as directory, mock.patch.object(routine_recovery, "trace", many):
            service, _value, _run_id = self.run_held(directory, assistant)
            state = self.state(service)
        # The loss is sealed with the completion it was met at, so the cursor never rolls back to the held step.
        self.assertEqual((self.status, record.incidents(state), state.runs), ("recovered", (), ()))
        self.assertTrue(state.notices[-1].protection_lost)
        assistant = Assistant([failed()], [{"outcome": "occurred", "result": RECORD}])
        # Still in time when the verifier is dispatched; out of time once it answered.
        ticks = iter([0.0, 0.0, 0.0])
        with (
            tempfile.TemporaryDirectory() as directory,
            mock.patch.object(routine_recovery, "trace", many),
            mock.patch.object(routine_recovery, "_clock", side_effect=lambda: next(ticks, 61.0)),
        ):
            service, _value, run_id = self.run_held(directory, assistant)
            state = self.state(service)
            cursor = self.cursor(service, run_id)
        # Out of time once verified: the completed step stays sealed, and the held run and its pause say it was lost.
        self.assertEqual((self.status, cursor.step, cursor.protection_lost), ("held", 2, True))
        self.assertTrue(record.run(state, run_id).protection_lost)
        self.assertEqual((state.notices[-1].detail["reason"], state.notices[-1].protection_lost), ("exhausted", True))

    def held_unmarked(self, directory: str):
        """A held run whose cursor sealed its lost protection, while the run's own mark never landed."""
        many = SimpleNamespace(secret_values=lambda *_args: [f"value-{index}" for index in range(200)])
        assistant = Assistant([failed()], [{"outcome": "occurred", "result": RECORD}])
        ticks = iter([0.0, 0.0, 0.0])
        with (
            mock.patch.object(routine_recovery, "trace", many),
            mock.patch.object(routine_recovery, "_clock", side_effect=lambda: next(ticks, 61.0)),
        ):
            service, value, run_id = self.run_held(directory, assistant)
        unmarked = dataclasses.replace(record.run(self.state(service), run_id), protection_lost=False)
        service.routine_store.update("team_1", lambda state: (record.replace_run(state, unmarked), None))
        return service, value, run_id

    def test_an_incidents_sealed_loss_marks_the_next_notice_it_publishes(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            service, _value, run_id = self.held_unmarked(directory)
            # Any other write reads no incident's evidence, so it leaves the incident as it is.
            with mock.patch.object(service.routine_store, "incident") as read:
                routine_state.update(service, "team_1", lambda state: (state, None))
            self.assertEqual((read.call_count, record.run(self.state(service), run_id).protection_lost), (0, False))
            # Evidence that cannot be read marks nothing; the next notice that can read it says the loss.
            with mock.patch.object(service.routine_store, "incident", side_effect=routine_store.RoutineStoreError("x")):
                routine_incident.pause(service, "team_1", run_id, "exhausted")
            self.assertFalse(self.state(service).notices[-1].protection_lost)
            routine_incident.pause(service, "team_1", run_id, "exhausted")
            state = self.state(service)
        self.assertEqual((record.run(state, run_id).protection_lost, state.notices[-1].protection_lost), (True, True))

    def test_setting_an_incident_aside_marks_its_sealed_loss_on_its_notice(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            service, _value, run_id = self.held_unmarked(directory)
            routine_incident.set_aside(
                service, "team_1", run_id, lambda state: routine_hold.skip_incident(state, run_id, int(time.time()))
            )
            notice = next(item for item in self.state(service).notices if item.notice_id == run_id)
        self.assertEqual((notice.outcome, notice.protection_lost), ("user-skipped", True))

    def test_deleting_a_routine_marks_its_incidents_sealed_loss_on_its_notice(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            service, value, run_id = self.held_unmarked(directory)
            service.delete_routine("team_1", value.routine_id)
            notice = next(item for item in self.state(service).notices if item.notice_id == run_id)
        self.assertEqual((notice.outcome, notice.protection_lost), ("user-skipped", True))


class BalanceCase(AutomaticCase):
    def held_with_balance(self, directory: str, seconds: int, assistant: Assistant):
        """A held run, without its automatic episode yet, whose remaining active time is ``seconds``."""
        service, _brain, value, run_id = self.held(directory, assistant, Brain())
        service.routine_store.update(
            "team_1",
            lambda state: (
                record.replace_run(
                    state, dataclasses.replace(routine_hold.incident(state, run_id), active_seconds_left=seconds)
                ),
                None,
            ),
        )
        return service, value, run_id


class RunBalanceTests(BalanceCase):
    """The episode's time is the recovery bound within the held run's remaining active time."""

    def test_a_nearly_spent_run_recovers_only_within_what_it_has_left(self) -> None:
        observed: list[tuple[int, float]] = []
        real_verify = routine_recovery.verify

        def watching(service, team_id, incident_id, token, protected=None):
            held = routine_hold.incident(service.routine_store.load(team_id), incident_id)
            left = service._routine_runs[incident_id].deadline - time.monotonic()
            observed.append((held.active_seconds_left, left))
            return real_verify(service, team_id, incident_id, token, protected)

        assistant = Assistant([failed()], [{"outcome": "inconclusive"}])
        with tempfile.TemporaryDirectory() as directory:
            service, _value, run_id = self.held_with_balance(directory, 5, assistant)
            run = mock.Mock(team_id="team_1", run_id=run_id, token=run_id)
            with mock.patch.object(routine_recovery, "verify", side_effect=watching):
                self.assertEqual(routine_recovery.automatic(service, run), "held")
            held = routine_hold.incident(self.state(service), run_id)
        ((active, left),) = observed
        # The episode was bounded by the 5 seconds the run had left, which its deadline keeps in memory only.
        self.assertEqual((active, held.active_seconds_left), (5, 5))
        self.assertTrue(0 < left <= 5)

    def test_verification_time_leaves_the_held_runs_balance(self) -> None:
        assistant = Assistant([failed()], [{"outcome": "inconclusive"}])
        elapsed = [0.0]
        real_verify = routine_recovery.verify

        def slow(service, team_id, incident_id, token, protected=None):
            elapsed[0] = 3.0
            return real_verify(service, team_id, incident_id, token, protected)

        with tempfile.TemporaryDirectory() as directory:
            service, _value, run_id = self.held_with_balance(directory, 50, assistant)
            run = mock.Mock(team_id="team_1", run_id=run_id, token=run_id)
            with (
                mock.patch.object(routine_recovery, "verify", side_effect=slow),
                mock.patch.object(routine_recovery, "_clock", side_effect=lambda: 1000.0 + elapsed[0]),
            ):
                self.assertEqual(routine_recovery.automatic(service, run), "held")
            held = routine_hold.incident(self.state(service), run_id)
        # A person's later resume of the held run starts from what verification left.
        self.assertEqual(held.active_seconds_left, 47)

    def test_verification_time_leaves_the_balance_when_the_team_cannot_be_read(self) -> None:
        assistant = Assistant([failed()], [{"outcome": "inconclusive"}])
        elapsed = [0.0]
        real_verify = routine_recovery.verify
        unreadable = bindings.DynamicAssistantError("the dynamic Assistant registry cannot be read")

        def unread(service, team_id, incident_id, token, protected=None):
            elapsed[0] = 3.0
            with mock.patch.object(service, "_team_assistants", side_effect=unreadable):
                return real_verify(service, team_id, incident_id, token, protected)

        with tempfile.TemporaryDirectory() as directory:
            service, _value, run_id = self.held_with_balance(directory, 50, assistant)
            run = mock.Mock(team_id="team_1", run_id=run_id, token=run_id)
            with (
                mock.patch.object(routine_recovery, "verify", side_effect=unread),
                mock.patch.object(routine_recovery, "_clock", side_effect=lambda: 1000.0 + elapsed[0]),
            ):
                self.assertEqual(routine_recovery.automatic(service, run), "held")
            held = routine_hold.incident(self.state(service), run_id)
        self.assertEqual(held.active_seconds_left, 47)

    def test_a_run_with_no_time_left_is_exhausted_before_any_work(self) -> None:
        assistant = Assistant([failed()], [{"outcome": "not_occurred"}])
        with tempfile.TemporaryDirectory() as directory:
            service, _value, run_id = self.held_with_balance(directory, 0, assistant)
            run = mock.Mock(team_id="team_1", run_id=run_id, token=run_id)
            outcome = routine_recovery.automatic(service, run)
            state = self.state(service)
        self.assertEqual(outcome, "held")
        self.assertNotIn("find-record", [action for action, _id in assistant.calls])
        self.assertEqual((state.notices[-1].outcome, state.notices[-1].detail["reason"]), ("paused", "exhausted"))

    def test_the_retry_runs_inside_the_recovery_allowance(self) -> None:
        assistant = Assistant([failed(), RECORD], [{"outcome": "not_occurred"}])
        bounds: list[int] = []
        real_continue = routine_recovery.continue_run

        def continuing(*args, **kwargs):
            bounds.append(kwargs["seconds"])
            return real_continue(*args, **kwargs)

        with (
            tempfile.TemporaryDirectory() as directory,
            mock.patch.object(routine_recovery, "continue_run", side_effect=continuing),
        ):
            self.run_held(directory, assistant)
        (bound,) = bounds
        self.assertEqual(self.status, "recovered")
        # The continuation that retries the step is bounded by what remains of the episode's time.
        self.assertTrue(0 <= bound <= 60)


class DeadlineTests(BalanceCase):
    def test_the_deadline_itself_cancels_a_blocked_verification_and_pauses_as_exhausted(self) -> None:
        released = threading.Event()

        class Blocking(Assistant):
            def __call__(self, team, assistant, action, payload, evidence):
                if action == "find-record":
                    # The verifier hangs; only the deadline's fail-stop releases it.
                    released.wait(10)
                return super().__call__(team, assistant, action, payload, evidence)

        assistant = Blocking([failed()], [{"outcome": "not_occurred"}])
        with tempfile.TemporaryDirectory() as directory:
            service, _value, run_id = self.held_with_balance(directory, 1, assistant)
            service.assistant_lifecycle._fail_stop_action = mock.Mock(side_effect=lambda _container: released.set())
            run = mock.Mock(team_id="team_1", run_id=run_id, token=run_id)
            started = time.monotonic()
            with service._exclusive_chat_turn("team_1") as token:
                run.token = token
                outcome = routine_recovery.automatic(service, run)
            elapsed = time.monotonic() - started
            state = self.state(service)
        # The one-second episode ended the hanging verifier at once, long before any watchdog pass.
        self.assertTrue(released.is_set())
        self.assertLess(elapsed, 5)
        self.assertEqual(outcome, "held")
        self.assertEqual((state.notices[-1].outcome, state.notices[-1].detail["reason"]), ("paused", "exhausted"))
        service.assistant_lifecycle._fail_stop_action.assert_called_once()

    def test_exhaustion_is_judged_on_the_clock_whatever_the_episode_found(self) -> None:
        assistant = Assistant([failed()], [{"outcome": "occurred", "result": RECORD}])
        with tempfile.TemporaryDirectory() as directory:
            service, _value, run_id = self.held_with_balance(directory, 30, assistant)
            run = mock.Mock(team_id="team_1", run_id=run_id, token=run_id)
            ticks = iter([0.0, 0.0, 0.0])
            with mock.patch.object(routine_recovery, "_clock", side_effect=lambda: next(ticks, 31.0)):
                outcome = routine_recovery.automatic(service, run)
            state = self.state(service)
        # Proven occurrence would continue, but the episode's time had run out: exhausted, not continued.
        self.assertEqual(outcome, "held")
        self.assertEqual((state.notices[-1].outcome, state.notices[-1].detail["reason"]), ("paused", "exhausted"))

    def test_a_deadline_whose_stop_cannot_be_proven_is_audited_and_still_expires(self) -> None:
        episode = routine_recovery._Episode(0, 0.0)
        blocked = routine_recovery.ApiProblem(503, "x", code="assistant-action-blocked")

        def unprovable(_service, _team, _run, _token, mark):
            # The deadline is marked as the cause, then the Action's fail-stop cannot be proven.
            mark()
            raise blocked

        with (
            mock.patch.object(routine_recovery.routine_run, "expire_routine_run", side_effect=unprovable),
            mock.patch.object(routine_recovery.local_audit, "record") as audited,
            routine_recovery._deadline(None, "team_1", "a" * 32, "token", episode),
        ):
            self.assertTrue(episode.expired.wait(5))
            for _attempt in range(100):
                if audited.called:
                    break
                time.sleep(0.01)
        # The timer thread has no request principal, so Team audits its own deadline.
        audited.assert_called_once_with(
            "routine-recovery",
            result="error",
            principal=routine_recovery.local_audit.TEAM,
            team_id="team_1",
            detail="deadline-stop",
        )


class ContinuationDeadlineTests(BalanceCase):
    def test_a_deadline_between_continuation_steps_holds_the_partial_run_and_pauses_as_exhausted(self) -> None:
        fired: list[object] = []

        real_resume = routine_recovery.routine_replay.ReplayRuntime.resume

        def resume(runtime, context, results):
            turn = real_resume(runtime, context, results)
            if runtime.cursor.continued and runtime.cursor.step == 2:
                # The retried step completed; the deadline passes before the next step is dispatched.
                fired[-1]()
            return turn

        assistant = Assistant([failed(), RECORD], [{"outcome": "not_occurred"}])
        controller_plan = (
            ("zones", "list-zones", LOOKUP_INPUT),
            ("create", "create-record", {"zone_id": ZONE, "name": "www"}),
            ("check", "list-zones", LOOKUP_INPUT),
        )
        with (
            tempfile.TemporaryDirectory() as directory,
            mock.patch.object(routine_recovery.threading, "Timer", captured_timer(fired)),
            mock.patch.object(routine_recovery.routine_replay.ReplayRuntime, "resume", resume),
            mock.patch.object(self, "plan", lambda service, *_steps: type(self).plan(service, *controller_plan)),
        ):
            service, value, run_id = self.run_held(directory, assistant)
            state = self.state(service)
            cursor = self.cursor(service, run_id)
        # Never ended as stopped: the partial run is held with its evidence and its Routine runs no further cycle.
        self.assertEqual(self.status, "held")
        self.assertEqual([item.run_id for item in record.incidents(state)], [run_id])
        self.assertEqual(cursor.step, 2)
        self.assertEqual((state.notices[-1].outcome, state.notices[-1].detail["reason"]), ("paused", "exhausted"))
        self.assertTrue(record.routine(state, value.routine_id).paused)
        self.assertEqual([action for action, _id in assistant.calls].count("create-record"), 2)

    def test_the_timer_is_set_for_what_is_left_and_nothing_starts_once_it_ran_out(self) -> None:
        delays: list[float] = []

        assistant = Assistant([failed()], [{"outcome": "not_occurred"}])
        with tempfile.TemporaryDirectory() as directory:
            service, _value, run_id = self.held_with_balance(directory, 30, assistant)
            run = mock.Mock(team_id="team_1", run_id=run_id, token=run_id)
            # Taken at 0, armed at 12, and already past its deadline when the episode would start work.
            ticks = iter([0.0, 12.0])
            with (
                mock.patch.object(routine_recovery.threading, "Timer", captured_timer([], delays)),
                mock.patch.object(routine_recovery, "_clock", side_effect=lambda: next(ticks, 30.0)),
            ):
                outcome = routine_recovery.automatic(service, run)
            state = self.state(service)
        self.assertEqual(delays, [18.0])
        self.assertEqual(outcome, "held")
        self.assertNotIn("find-record", [action for action, _id in assistant.calls])
        self.assertEqual((state.notices[-1].outcome, state.notices[-1].detail["reason"]), ("paused", "exhausted"))

    def test_a_retry_with_no_time_left_is_never_dispatched(self) -> None:
        assistant = Assistant([failed(), RECORD], [{"outcome": "not_occurred"}])
        with tempfile.TemporaryDirectory() as directory:
            service, _value, run_id = self.held_with_balance(directory, 30, assistant)
            run = mock.Mock(team_id="team_1", run_id=run_id, token=run_id)
            # In time through the verification, with less than a second left for the retry.
            ticks = iter([0.0, 0.0, 0.0, 0.0, 29.5])
            with (
                service._exclusive_chat_turn("team_1") as token,
                mock.patch.object(routine_recovery, "_clock", side_effect=lambda: next(ticks, 29.5)),
            ):
                run.token = token
                outcome = routine_recovery.automatic(service, run)
            state = self.state(service)
        self.assertEqual(outcome, "held")
        self.assertEqual([action for action, _id in assistant.calls].count("create-record"), 1)
        self.assertEqual((state.notices[-1].outcome, state.notices[-1].detail["reason"]), ("paused", "exhausted"))

    def test_a_low_balance_retry_continues_with_the_time_its_episode_has_left(self) -> None:
        assistant = Assistant([failed(), RECORD], [{"outcome": "not_occurred"}])
        balances: list[tuple[int, int]] = []
        real_continue = routine_recovery.continue_run

        def continuing(service, team_id, incident_id, token, progress=None, *, seconds):
            held = routine_hold.incident(service.routine_store.load(team_id), incident_id)
            balances.append((held.active_seconds_left, seconds))
            return real_continue(service, team_id, incident_id, token, progress, seconds=seconds)

        with tempfile.TemporaryDirectory() as directory:
            # The episode had only the 5 seconds the run had left; the retry still runs inside them.
            service, _value, run_id = self.held_with_balance(directory, 5, assistant)
            with (
                service._exclusive_chat_turn("team_1") as token,
                mock.patch.object(routine_recovery, "continue_run", side_effect=continuing),
            ):
                run = mock.Mock(team_id="team_1", run_id=run_id, token=token)
                outcome = routine_recovery.automatic(service, run)
            state = self.state(service)
        self.assertEqual(outcome, "recovered")
        self.assertEqual([action for action, _id in assistant.calls].count("create-record"), 2)
        # The run started its continuation with what the episode had left, never more than its 5 seconds.
        ((active, handed),) = balances
        self.assertEqual(active, 5)
        self.assertTrue(0 < handed <= 5)
        self.assertEqual((record.incidents(state), state.runs, state.notices[-1].outcome), ((), (), "recovered"))

    def test_a_deadline_just_before_the_continuation_registers_still_holds_and_pauses(self) -> None:
        fired: list[object] = []

        real_registered = routine_recovery.routine_run.registered
        calls: list[str] = []

        def registering(service, team_id, run_id, token, seconds):
            calls.append(team_id)
            if len(calls) == 3:
                # The run and then its episode registered first; now the run was reopened, and the deadline passes
                # before its continuation registers.
                self.assertEqual(record.incidents(service.routine_store.load(team_id)), ())
                fired[-1]()
            return real_registered(service, team_id, run_id, token, seconds)

        assistant = Assistant([failed(), RECORD], [{"outcome": "not_occurred"}])
        with (
            tempfile.TemporaryDirectory() as directory,
            mock.patch.object(routine_recovery.threading, "Timer", captured_timer(fired)),
            mock.patch.object(routine_recovery.routine_run, "registered", side_effect=registering),
        ):
            service, value, run_id = self.run_held(directory, assistant)
            state = self.state(service)
        # Out of time, not stopped: the partial run is held again and its Routine pauses as exhausted.
        self.assertEqual(self.status, "held")
        self.assertEqual([item.run_id for item in record.incidents(state)], [run_id])
        self.assertEqual((state.notices[-1].outcome, state.notices[-1].detail["reason"]), ("paused", "exhausted"))
        self.assertTrue(record.routine(state, value.routine_id).paused)

    def test_a_deadline_that_cancels_the_continuation_before_it_reopens_still_pauses_as_exhausted(self) -> None:
        fired: list[object] = []

        real_continued = routine_recovery.routine_cursor.continued

        def continued(cursor):
            # The deadline passes while the continuation is being prepared, before the run reopens.
            fired[-1]()
            return real_continued(cursor)

        assistant = Assistant([failed(), RECORD], [{"outcome": "not_occurred"}])
        with (
            tempfile.TemporaryDirectory() as directory,
            mock.patch.object(routine_recovery.threading, "Timer", captured_timer(fired)),
            mock.patch.object(routine_recovery.routine_cursor, "continued", side_effect=continued),
        ):
            service, value, run_id = self.run_held(directory, assistant)
            state = self.state(service)
        # The refused continuation is no plain hold: the incident and its evidence stay, and the Routine pauses.
        self.assertEqual(self.status, "held")
        self.assertEqual([item.run_id for item in record.incidents(state)], [run_id])
        self.assertEqual((state.notices[-1].outcome, state.notices[-1].detail["reason"]), ("paused", "exhausted"))
        self.assertTrue(record.routine(state, value.routine_id).paused)
        self.assertEqual([action for action, _id in assistant.calls].count("create-record"), 1)

    def test_an_episode_that_cannot_register_holds_the_run(self) -> None:
        assistant = Assistant([failed()], [{"outcome": "not_occurred"}])
        with tempfile.TemporaryDirectory() as directory:
            service, _value, run_id = self.held_with_balance(directory, 30, assistant)
            with service._active_chat_guard:
                # A Stop found the run unregistered and fenced it out.
                service._routine_halting.add(run_id)
            run = mock.Mock(team_id="team_1", run_id=run_id, token=run_id)
            self.assertEqual(routine_recovery.automatic(service, run), "held")
        self.assertNotIn("find-record", [action for action, _id in assistant.calls])

    def cut_after_the_last_step(self, cause: str, exhaust: str = ""):
        """Run a retried continuation and cancel it, by its deadline or a person, once every step is sealed complete."""
        fired: list[object] = []
        box: list[object] = []

        real_resume = routine_recovery.routine_replay.ReplayRuntime.resume

        def resume(runtime, context, results):
            turn = real_resume(runtime, context, results)
            if runtime.cursor.continued and runtime.cursor.step == 2:
                if exhaust:
                    # The deadline also spent the run's active time, or let its lease lapse.
                    changes = {"time": {"active_seconds_left": 0}, "lease": {"lease_expires_at": 1}}[exhaust]
                    run_id = runtime.cursor.binding.run_id
                    box[0].routine_store.update(
                        "team_1",
                        lambda state: (
                            record.replace_run(state, dataclasses.replace(record.run(state, run_id), **changes)),
                            None,
                        ),
                    )
                # Every step is sealed complete; the cancellation lands before the run's end is recorded.
                if cause == "deadline":
                    fired[-1]()
                elif cause == "person":
                    routine_recovery.routine_run.stop_routine_run(box[0], "team_1", runtime.cursor.binding.run_id)
            return turn

        def commit(team_id, token):
            # The deadline passes exactly as the completed run commits its end.
            if cause == "commit" and box[0]._routine_runs:
                fired[-1]()
            return real_commit[0](team_id, token)

        real_commit: list[object] = []

        original = self.service

        def service(*args, **kwargs):
            controller, value = original(*args, **kwargs)
            box.append(value)
            real_commit.append(value._commit_chat_terminal)
            value._commit_chat_terminal = commit
            return controller, value

        real_routine = self.routine

        def failing_streak(service, **kwargs):
            # Two no-effect failures already ran before this cycle.
            value = real_routine(service, **kwargs)
            routine_fixture.update_routine(service, value.routine_id, failures=2)
            return value

        assistant = Assistant([failed(), RECORD], [{"outcome": "not_occurred"}])
        with (
            tempfile.TemporaryDirectory() as directory,
            mock.patch.object(self, "service", service),
            mock.patch.object(self, "routine", failing_streak),
            mock.patch.object(routine_recovery.threading, "Timer", captured_timer(fired)),
            mock.patch.object(routine_recovery.routine_replay.ReplayRuntime, "resume", resume),
        ):
            service_value, value, _run_id = self.run_held(directory, assistant)
            state = self.state(service_value)
        return state, record.routine(state, value.routine_id)

    def test_a_deadline_after_the_last_step_records_completion_from_the_sealed_cursor(self) -> None:
        state, routine_value = self.cut_after_the_last_step("deadline")
        self.assertEqual((self.status, record.incidents(state)), ("recovered", ()))
        # Recorded complete from the sealed cursor, with its Actions, and its failure streak reset.
        self.assertEqual(state.notices[-1].outcome, "recovered")
        self.assertEqual(state.notices[-1].detail["plan"]["actions"][-1][:2], ["shimpz-cloudflare", "create-record"])
        self.assertEqual((routine_value.failures, routine_value.paused), (0, False))

    def test_a_persons_stop_after_the_last_step_still_stops_the_run(self) -> None:
        state, routine_value = self.cut_after_the_last_step("person")
        self.assertEqual((self.status, record.incidents(state), state.notices[-1].outcome), ("stopped", (), "stopped"))
        # A person's Stop is neither success nor failure: the streak stays as it was.
        self.assertEqual(routine_value.failures, 2)

    def test_a_deadline_as_the_completed_run_commits_its_end_still_records_completion(self) -> None:
        state, routine_value = self.cut_after_the_last_step("commit")
        self.assertEqual(
            (self.status, state.notices[-1].outcome, routine_value.failures), ("recovered", "recovered", 0)
        )

    def test_a_deadline_completion_with_no_time_or_lease_left_is_still_recorded_complete(self) -> None:
        for cause in ("deadline", "commit"):
            for exhaust in ("time", "lease"):
                with self.subTest(cause=cause, exhaust=exhaust):
                    state, routine_value = self.cut_after_the_last_step(cause, exhaust)
                    # Not failed with no Actions: the sealed completion is recorded and the streak reset.
                    self.assertEqual((self.status, state.notices[-1].outcome), ("recovered", "recovered"))
                    self.assertEqual(
                        state.notices[-1].detail["plan"]["actions"][-1][:2], ["shimpz-cloudflare", "create-record"]
                    )
                    self.assertEqual((state.runs, routine_value.failures), ((), 0))

    def test_a_sealed_completion_never_touches_a_run_whose_lease_changed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            service, _brain, value, run_id = self.held(directory, Assistant([failed()], []))
            run = routine_recovery.routine_run.LeasedRun("team_1", run_id, record.Lease("0" * 64, "k"), "token", value)
            with self.assertRaises(routine_recovery.ApiProblem) as caught:
                routine_recovery.routine_run.complete_sealed(service, run, None)
        self.assertEqual(caught.exception.code, "routine-lease-invalid")
