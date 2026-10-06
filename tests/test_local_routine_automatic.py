"""A held run's one automatic recovery episode: verification first, the Brain only on proven absence (ADR-0092)."""

from __future__ import annotations

import dataclasses
import tempfile
import threading
import time
import unittest
from typing import ClassVar
from unittest import mock

from local_assistant_fixture import mutating_spec
from test_local_chat_scope import LOOKUP_INPUT
from test_local_routine_compiled import ZONE
from test_local_routine_recovery import RECORD, Assistant, RecoveryCase, failed
from test_local_routine_service import API_KEY, ASSISTANT, KEY

from inference import client as inference_client
from inference import config as inference_config
from inference import recovery as inference_recovery
from local import authority as local_authority
from local.routine import recovery as routine_recovery
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
    """The Brain as the automatic episode may reach it: only its one recovery decision, each answer in turn."""

    def __init__(self, *decisions: str) -> None:
        self.decisions = list(decisions)
        self.asked: list[dict[str, object]] = []

    def routine_recovery(self, payload, _provider, _model):
        self.asked.append(payload)
        return {"decision": self.decisions.pop(0)}

    def __getattr__(self, name: str):
        raise AssertionError(f"the episode asked the Brain to {name}")


class Switching(Assistant):
    """An Assistant whose first create runs while the Team's model configuration changes to another provider."""

    service = None

    def __call__(self, team, assistant, action, payload, evidence):
        if action == "create-record" and self.service is not None:
            self.service.inference_store.save("team_1", inference_config.normalize("anthropic"))
            self.service = None
        return super().__call__(team, assistant, action, payload, evidence)


class AutomaticCase(RecoveryCase):
    def run_held(self, directory: str, assistant: Assistant, brain: Brain, key: str = API_KEY):
        """Claim and run with the model key Admin sends; the hold runs its automatic episode at once."""
        service, _brain, value, run_id = self.held_automatic(directory, assistant, brain, key)
        return service, value, run_id

    def held_automatic(self, directory, assistant, brain, key):
        controller, service = self.service(directory, brain)
        current = controller.registry[ASSISTANT]
        controller.registry[ASSISTANT] = dataclasses.replace(
            mutating_spec(current.image), provenance=current.provenance, platform=current.platform
        )
        controller.assistant_lifecycle.invoke = assistant
        # The Assistant may reach the Team's state while its Action runs, as a person changing it meanwhile would.
        assistant.service = service
        plan = self.plan(
            service,
            ("zones", "list-zones", LOOKUP_INPUT),
            ("create", "create-record", {"zone_id": ZONE, "name": "www"}),
        )
        value = self.routine(service, plan=plan)
        claim = service.claim_routine_run()
        evidence = local_authority.RoutineEvidence(KEY, record.lease_sha256(claim["lease_token"]), "a" * 32, 0)
        self.status = service.run_routine(
            "team_1",
            claim["run_id"],
            evidence,
            (claim["revision"], claim["plan_digest"], claim["mode"]),
            ("openai", key),
        )["status"]
        return service, brain, value, claim["run_id"]


class AutomaticTests(AutomaticCase):
    def test_proven_absence_asks_once_and_retries_the_same_operation(self) -> None:
        brain = Brain("retry")
        assistant = Assistant([failed(), RECORD], [{"outcome": "not_occurred"}])
        with tempfile.TemporaryDirectory() as directory:
            service, _value, _run_id = self.run_held(directory, assistant, brain)
            state = self.state(service)
        self.assertEqual((self.status, state.incidents, state.runs), ("recovered", (), ()))
        (asked,) = brain.asked
        self.assertEqual(
            (asked["proof"], asked["step"]), ("not_occurred", {"assistant": ASSISTANT, "action": "create-record"})
        )
        # The step's sanitized diagnostic is the Brain's untrusted evidence; the 404 alone never proved anything.
        self.assertEqual(asked["diagnostics"][0]["failure"]["http_status"], 404)
        creates = [operation for action, operation in assistant.calls if action == "create-record"]
        self.assertEqual((len(creates), creates[0]), (2, creates[1]))

    def test_the_decisions_reported_tokens_reach_the_recovered_runs_notice(self) -> None:
        models = [{"provider": "openai", "model": "gpt-6-luna", "input_tokens": 10, "output_tokens": 5}]
        assistant = Assistant([failed(), RECORD], [{"outcome": "not_occurred"}])
        with (
            tempfile.TemporaryDirectory() as directory,
            mock.patch.object(routine_recovery.brain_usage, "since", return_value=models),
        ):
            service, _value, run_id = self.run_held(directory, assistant, Brain("retry"))
            state = self.state(service)
        notice = next(item for item in state.notices if item.run_id == run_id)
        self.assertEqual((self.status, notice.outcome, notice.usage["models"]), ("recovered", "recovered", models))

    def test_a_proven_occurrence_continues_with_no_model_call(self) -> None:
        brain = Brain()
        assistant = Assistant([failed()], [{"outcome": "occurred", "result": RECORD}])
        with tempfile.TemporaryDirectory() as directory:
            service, _value, _run_id = self.run_held(directory, assistant, brain)
            state = self.state(service)
        self.assertEqual((self.status, brain.asked, state.incidents), ("recovered", [], ()))

    def test_inconclusive_evidence_holds_for_the_card_without_asking_the_brain(self) -> None:
        brain = Brain()
        assistant = Assistant([failed()], [{"outcome": "inconclusive"}])
        with tempfile.TemporaryDirectory() as directory:
            service, value, run_id = self.run_held(directory, assistant, brain)
            state = self.state(service)
            cursor = self.cursor(service, run_id)
        self.assertEqual((self.status, brain.asked), ("held", []))
        self.assertEqual([item.incident_id for item in state.incidents], [run_id])
        self.assertFalse(record.routine(state, value.routine_id).paused)
        self.assertEqual((cursor.remaining("episodes"), cursor.remaining("verifications")), (0, 2))

    def test_ask_holds_and_pause_or_an_unavailable_decision_also_pauses_the_routine(self) -> None:
        cases = ((("ask",), API_KEY, None), (("pause",), API_KEY, "decided"), ((), "", "unavailable"))
        for decisions, key, reason in cases:
            paused = reason is not None
            brain = Brain(*decisions)
            assistant = Assistant([failed()], [{"outcome": "not_occurred"}])
            with tempfile.TemporaryDirectory() as directory, self.subTest(decisions=decisions, key=bool(key)):
                service, value, run_id = self.run_held(directory, assistant, brain, key)
                state = self.state(service)
                cursor = self.cursor(service, run_id)
                self.assertEqual(self.status, "held")
                self.assertEqual(record.routine(state, value.routine_id).paused, paused)
                # The held run's one notice says it is held, or why recovery paused its Routine.
                notice = state.notices[-1]
                step = {
                    "assistant_id": ASSISTANT,
                    "action": "create-record",
                    "position": {"phase": "replay", "step": 2},
                    "steps": 2,
                }
                expected = ("held", step) if reason is None else ("paused", {**step, "reason": reason})
                self.assertEqual((notice.notice_id, (notice.outcome, notice.detail)), (run_id, expected))
                # Each call and its whole output cap are paid before the call; with no key, nothing is asked.
                self.assertEqual(len(brain.asked), len(decisions))
                self.assertEqual((cursor.remaining("model_calls"), cursor.remaining("output_tokens")), (3, 3072))

    def test_the_episode_runs_once_and_the_retry_never_repeats(self) -> None:
        brain = Brain("retry", "retry")
        assistant = Assistant([failed(), failed()], [{"outcome": "not_occurred"}, {"outcome": "not_occurred"}])
        with tempfile.TemporaryDirectory() as directory:
            service, _value, run_id = self.run_held(directory, assistant, brain)
            cursor = self.cursor(service, run_id)
        self.assertEqual((self.status, len(brain.asked)), ("held", 1))
        self.assertEqual([action for action, _id in assistant.calls].count("create-record"), 2)
        self.assertEqual((cursor.remaining("episodes"), cursor.remaining("retries")), (0, 0))

    def test_an_episode_past_its_active_time_stops_and_a_restart_never_starts_another(self) -> None:
        brain = Brain("retry")
        assistant = Assistant([failed()], [{"outcome": "not_occurred"}])
        ticks = iter([0.0])
        with tempfile.TemporaryDirectory() as directory:
            with mock.patch.object(routine_recovery, "_clock", side_effect=lambda: next(ticks, 61.0)):
                service, _value, run_id = self.run_held(directory, assistant, brain)
            cursor = self.cursor(service, run_id)
            run = mock.Mock(team_id="team_1", run_id=run_id, provider="openai", token=run_id)
            again = routine_recovery.automatic(service, run, API_KEY)
            state = self.state(service)
        self.assertEqual((self.status, brain.asked, again), ("held", [], "held"))
        self.assertEqual(cursor.remaining("recovery_seconds"), 0)
        # Running out of recovery time publishes the pause and its reason, not a plain hold.
        self.assertEqual((state.notices[-1].outcome, state.notices[-1].detail["reason"]), ("paused", "exhausted"))

    def test_the_episode_time_is_reserved_durably_first_and_runs_under_a_cancellable_deadline(self) -> None:
        brain = Brain("ask")
        assistant = Assistant([failed()], [{"outcome": "not_occurred"}])
        observed: list[tuple[int, float]] = []
        real_verify = routine_recovery.verify

        def watching(service, team_id, incident_id, token, protected=None):
            # While the episode works, its whole time is already spent durably and it is registered to its deadline.
            cursor = routine_recovery.routine_incident.open_recovery(service, team_id, incident_id).cursor
            registration = service._routine_runs[incident_id]
            observed.append((cursor.remaining("recovery_seconds"), registration.deadline - time.monotonic()))
            return real_verify(service, team_id, incident_id, token, protected)

        with (
            tempfile.TemporaryDirectory() as directory,
            mock.patch.object(routine_recovery, "verify", side_effect=watching),
        ):
            service, _value, run_id = self.run_held(directory, assistant, brain)
            cursor = self.cursor(service, run_id)
            registered = run_id in service._routine_runs
        ((remaining, left),) = observed
        self.assertEqual(remaining, 0)
        self.assertTrue(55 < left <= 60)
        # The unused part comes back when the episode ends; at least a second is always charged.
        self.assertTrue(0 < cursor.remaining("recovery_seconds") <= 59)
        self.assertFalse(registered)

    def test_a_crash_inside_the_episode_never_refills_its_time(self) -> None:
        brain = Brain("ask")
        assistant = Assistant([failed()], [{"outcome": "not_occurred"}])
        with (
            tempfile.TemporaryDirectory() as directory,
            mock.patch.object(routine_recovery, "_release"),
        ):
            # The process dies before the unused time is returned: the reservation stays spent.
            service, _value, run_id = self.run_held(directory, assistant, brain)
            cursor = self.cursor(service, run_id)
        self.assertEqual((cursor.remaining("recovery_seconds"), cursor.remaining("episodes")), (0, 0))


class AutomaticEdgeTests(AutomaticCase):
    def test_an_exhausted_decision_or_unreadable_diagnostics_pause_without_asking(self) -> None:
        for name in ("exhausted", "diagnostics"):
            brain = Brain("ask")
            assistant = Assistant([failed()], [{"outcome": "not_occurred"}])
            patches = {
                "exhausted": mock.patch.object(
                    routine_recovery.routine_cursor, "spend", side_effect=_spend_without_calls
                ),
                "diagnostics": mock.patch.object(
                    routine_recovery.routine_diagnostics.DiagnosticStore,
                    "read",
                    side_effect=routine_recovery.routine_diagnostics.DiagnosticStoreError("down"),
                ),
            }
            with tempfile.TemporaryDirectory() as directory, self.subTest(name=name), patches[name]:
                service, value, _run_id = self.run_held(directory, assistant, brain)
                state = self.state(service)
                paused = record.routine(state, value.routine_id).paused
                self.assertEqual(self.status, "held")
                self.assertTrue(paused)
                # Unreadable or corrupt diagnostics pause the Routine without asking the model anything.
                expected = {"exhausted": "exhausted", "diagnostics": "evidence"}[name]
                self.assertEqual(state.notices[-1].detail["reason"], expected)
                self.assertEqual(brain.asked, [])

    def test_absent_or_expired_diagnostics_are_simply_left_out(self) -> None:
        brain = Brain("ask")
        assistant = Assistant([failed()], [{"outcome": "not_occurred"}])
        with (
            tempfile.TemporaryDirectory() as directory,
            mock.patch.object(routine_recovery.routine_diagnostics.DiagnosticStore, "read", return_value=()),
        ):
            service, value, _run_id = self.run_held(directory, assistant, brain)
            state = self.state(service)
        self.assertEqual(brain.asked[0]["diagnostics"], [])
        self.assertFalse(record.routine(state, value.routine_id).paused)

    def test_an_unconfigured_team_has_no_decision(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            service, _brain, _value, run_id = self.held(directory, Assistant([failed()], []))
            down = routine_recovery.inference_config.InferenceConfigError("down")
            with mock.patch.object(service.inference_store, "load", side_effect=down):
                decided = routine_recovery._decide(service, "team_1", run_id, ("openai", API_KEY), None)
                self.assertEqual(decided, "unavailable")

    def test_a_key_is_never_sent_once_the_team_switched_to_another_provider(self) -> None:
        brain = Brain("retry")
        assistant = Switching([failed(), RECORD], [{"outcome": "not_occurred"}])
        with (
            tempfile.TemporaryDirectory() as directory,
            mock.patch.object(routine_recovery.local_audit, "record_request") as audited,
        ):
            service, value, run_id = self.run_held(directory, assistant, brain)
            # The Team's provider changed while the Action ran: the OpenAI key never reaches the Brain for Anthropic,
            # no model call is charged, and the held run pauses its Routine as a decision that could not be made.
            self.assertEqual((brain.asked, self.status), ([], "held"))
            state = self.state(service)
            cursor = self.cursor(service, run_id)
        self.assertEqual((cursor.remaining("model_calls"), cursor.remaining("output_tokens")), (4, 4096))
        self.assertTrue(record.routine(state, value.routine_id).paused)
        self.assertEqual(state.notices[-1].detail["reason"], "unavailable")
        self.assertEqual([action for action, _id in assistant.calls].count("create-record"), 1)
        audited.assert_any_call(
            "routine-recovery", result="denied", team_id="team_1", detail="inference-provider-mismatch"
        )

    def test_time_running_out_after_evidence_or_a_failing_assessment_holds_the_run(self) -> None:
        assistant = Assistant([failed()], [{"outcome": "occurred", "result": RECORD}])
        # Still in time when the verifier is dispatched; out of time once it answered.
        ticks = iter([0.0, 0.0, 0.0])
        with tempfile.TemporaryDirectory() as directory:
            with mock.patch.object(routine_recovery, "_clock", side_effect=lambda: next(ticks, 61.0)):
                service, _value, run_id = self.run_held(directory, assistant, Brain())
            self.assertEqual(self.status, "held")
            self.assertEqual(self.cursor(service, run_id).step, 2)
        assistant = Assistant([failed()], [])
        drift = routine_recovery.ApiProblem(409, "x", code="routine-drift")
        with (
            tempfile.TemporaryDirectory() as directory,
            mock.patch.object(routine_recovery, "verify", side_effect=drift),
        ):
            self.run_held(directory, assistant, Brain())
        self.assertEqual(self.status, "held")
        with (
            mock.patch.object(routine_recovery.routine_incident, "open_recovery", side_effect=drift),
            mock.patch.object(routine_recovery, "_clock", return_value=1.0),
        ):
            self.assertIsNone(routine_recovery._reserve(None, "team_1", "a" * 32))
            reservation = routine_recovery._Reservation(60, "g", 0.0)
            self.assertIsNone(routine_recovery._release(None, "team_1", "a" * 32, reservation))

    def test_an_episode_that_used_all_its_time_returns_nothing(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            service, _value, run_id = self.run_held(
                directory, Assistant([failed()], [{"outcome": "inconclusive"}]), Brain()
            )
            cursor = self.cursor(service, run_id)
            spent = routine_recovery.routine_cursor.spend(
                cursor, "recovery_seconds", cursor.remaining("recovery_seconds")
            )
            service.routine_store.put_cursor("team_1", spent)
            with mock.patch.object(routine_recovery, "_clock", return_value=100.0):
                routine_recovery._release(service, "team_1", run_id, routine_recovery._Reservation(60, "g", 0.0))
            self.assertEqual(self.cursor(service, run_id).remaining("recovery_seconds"), 0)
            with self.assertRaisesRegex(routine_recovery.routine_cursor.CursorError, "cursor-budget-invalid"):
                routine_recovery.routine_cursor.refund(spent, "recovery_seconds", 0)
            with self.assertRaisesRegex(routine_recovery.routine_cursor.CursorError, "cursor-invalid"):
                routine_recovery.routine_cursor.refund(cursor, "recovery_seconds", 61)


_REAL_SPEND = routine_recovery.routine_cursor.spend


def _spend_without_calls(cursor, budget, amount):
    if budget == "model_calls":
        raise routine_recovery.routine_cursor.CursorError("cursor-budget-exhausted")
    return _REAL_SPEND(cursor, budget, amount)


class DecisionClientTests(unittest.TestCase):
    SUBJECT: ClassVar[dict[str, object]] = {
        "routine": {"name": "N", "request": "R"},
        "step": {"assistant": "a", "action": "b"},
        "proof": "no_effect",
    }

    def client(self, answer):
        client = inference_client.BrainRuntimeClient.__new__(inference_client.BrainRuntimeClient)
        usage = dict.fromkeys(inference_client.brain_usage.FIELDS, 0)
        client._post = mock.Mock(return_value={**answer, "usage": usage})
        return client

    def test_only_a_closed_decision_from_a_valid_request_is_admitted(self) -> None:
        client = self.client({"decision": "retry"})
        with mock.patch.object(inference_client.brain_usage, "record"):
            decided = inference_recovery.decide(client, ("openai", "gpt-6-luna", "sk-x"), "pt", self.SUBJECT, ())
            self.assertEqual(decided, "retry")
            self.assertEqual(client._post.call_args.args[0], "/v1/routine-recovery")
            for answer in ({"decision": "verify"}, {"decision": "ask", "x": 1}):
                with self.subTest(answer=answer), self.assertRaises(inference_client.BrainRuntimeError):
                    inference_recovery.decide(self.client(answer), ("openai", "m", "k"), None, self.SUBJECT, ())
        for credentials, locale, subject, diagnostics in (
            (("other", "m", "k"), None, self.SUBJECT, ()),
            (("openai", "bad model", "k"), None, self.SUBJECT, ()),
            (("openai", "m", ""), None, self.SUBJECT, ()),
            (("openai", "m", "k"), "xx", self.SUBJECT, ()),
            (("openai", "m", "k"), None, {**self.SUBJECT, "proof": "occurred"}, ()),
            (("openai", "m", "k"), None, self.SUBJECT, ({},) * 9),
        ):
            with (
                self.subTest(credentials=credentials, locale=locale),
                self.assertRaises(inference_client.BrainRuntimeError),
            ):
                inference_recovery.decide(client, credentials, locale, subject, diagnostics)


class BalanceCase(AutomaticCase):
    def held_with_balance(self, directory: str, seconds: int, assistant: Assistant, brain: Brain | None = None):
        """A held run, without its automatic episode yet, whose remaining active time is ``seconds``."""
        service, _brain, value, run_id = self.held(directory, assistant, brain or Brain())
        service.routine_store.update(
            "team_1",
            lambda state: (
                routine_hold._replace_incident(
                    state, dataclasses.replace(routine_hold.incident(state, run_id), active_seconds_left=seconds)
                ),
                None,
            ),
        )
        return service, value, run_id


class RunBalanceTests(BalanceCase):
    """Recovery time comes out of both the recovery budget and the held run's remaining active time."""

    def test_a_nearly_spent_run_reserves_only_what_it_has_left_and_gets_the_rest_back(self) -> None:
        observed: list[tuple[int, int, float]] = []
        real_verify = routine_recovery.verify

        def watching(service, team_id, incident_id, token, protected=None):
            cursor = routine_recovery.routine_incident.open_recovery(service, team_id, incident_id).cursor
            held = routine_hold.incident(service.routine_store.load(team_id), incident_id)
            left = service._routine_runs[incident_id].deadline - time.monotonic()
            observed.append((cursor.remaining("recovery_seconds"), held.active_seconds_left, left))
            return real_verify(service, team_id, incident_id, token, protected)

        assistant = Assistant([failed()], [{"outcome": "inconclusive"}])
        with tempfile.TemporaryDirectory() as directory:
            service, _value, run_id = self.held_with_balance(directory, 5, assistant)
            run = mock.Mock(team_id="team_1", run_id=run_id, provider="openai", token=run_id)
            with mock.patch.object(routine_recovery, "verify", side_effect=watching):
                self.assertEqual(routine_recovery.automatic(service, run, API_KEY), "held")
            cursor = self.cursor(service, run_id)
            held = routine_hold.incident(self.state(service), run_id)
        ((recovery, active, left),) = observed
        # Only the 5 seconds the run had left were reserved, from both balances, and the episode was bounded by them.
        self.assertEqual((recovery, active), (55, 0))
        self.assertTrue(0 < left <= 5)
        # The unused part came back to both; at least a second stays charged.
        self.assertTrue(55 < cursor.remaining("recovery_seconds") <= 59)
        self.assertTrue(0 < held.active_seconds_left <= 4)

    def test_a_run_with_no_time_left_is_exhausted_before_any_work(self) -> None:
        brain = Brain("retry")
        assistant = Assistant([failed()], [{"outcome": "not_occurred"}])
        with tempfile.TemporaryDirectory() as directory:
            service, _value, run_id = self.held_with_balance(directory, 0, assistant, brain)
            run = mock.Mock(team_id="team_1", run_id=run_id, provider="openai", token=run_id)
            outcome = routine_recovery.automatic(service, run, API_KEY)
            state = self.state(service)
        self.assertEqual((outcome, brain.asked), ("held", []))
        self.assertNotIn("find-record", [action for action, _id in assistant.calls])
        self.assertEqual((state.notices[-1].outcome, state.notices[-1].detail["reason"]), ("paused", "exhausted"))

    def test_the_retry_runs_inside_the_recovery_allowance(self) -> None:
        brain = Brain("retry")
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
            self.run_held(directory, assistant, brain)
        (bound,) = bounds
        self.assertEqual(self.status, "recovered")
        # The continuation that retries the step is bounded by what remains of the episode's reservation.
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

        brain = Brain("retry")
        assistant = Blocking([failed()], [{"outcome": "not_occurred"}])
        with tempfile.TemporaryDirectory() as directory:
            service, _value, run_id = self.held_with_balance(directory, 1, assistant, brain)
            service.assistant_lifecycle._fail_stop_action = mock.Mock(side_effect=lambda _container: released.set())
            run = mock.Mock(team_id="team_1", run_id=run_id, provider="openai", token=run_id)
            started = time.monotonic()
            with service._exclusive_chat_turn("team_1") as token:
                run.token = token
                outcome = routine_recovery.automatic(service, run, API_KEY)
            elapsed = time.monotonic() - started
            state = self.state(service)
        # The one-second reservation ended the hanging verifier at once, long before any watchdog pass.
        self.assertTrue(released.is_set())
        self.assertLess(elapsed, 5)
        self.assertEqual((outcome, brain.asked), ("held", []))
        self.assertEqual((state.notices[-1].outcome, state.notices[-1].detail["reason"]), ("paused", "exhausted"))
        service.assistant_lifecycle._fail_stop_action.assert_called_once()

    def test_exhaustion_is_judged_on_the_clock_whatever_the_episode_found(self) -> None:
        assistant = Assistant([failed()], [{"outcome": "occurred", "result": RECORD}])
        with tempfile.TemporaryDirectory() as directory:
            service, _value, run_id = self.held_with_balance(directory, 30, assistant)
            run = mock.Mock(team_id="team_1", run_id=run_id, provider="openai", token=run_id)
            ticks = iter([0.0, 0.0, 0.0])
            with mock.patch.object(routine_recovery, "_clock", side_effect=lambda: next(ticks, 31.0)):
                outcome = routine_recovery.automatic(service, run, API_KEY)
            state = self.state(service)
        # Proven occurrence would continue, but the reservation had run out: exhausted, not continued.
        self.assertEqual(outcome, "held")
        self.assertEqual((state.notices[-1].outcome, state.notices[-1].detail["reason"]), ("paused", "exhausted"))

    def test_a_deadline_whose_stop_cannot_be_proven_is_audited_and_still_expires(self) -> None:
        reservation = routine_recovery._Reservation(0, "g", 0.0)
        blocked = routine_recovery.ApiProblem(503, "x", code="assistant-action-blocked")

        def unprovable(_service, _team, _run, _token, mark):
            # The deadline is marked as the cause, then the Action's fail-stop cannot be proven.
            mark()
            raise blocked

        with (
            mock.patch.object(routine_recovery.routine_run, "expire_routine_run", side_effect=unprovable),
            mock.patch.object(routine_recovery.local_audit, "record_request") as audited,
            routine_recovery._deadline(None, "team_1", "a" * 32, "token", reservation),
        ):
            self.assertTrue(reservation.expired.wait(5))
            for _attempt in range(100):
                if audited.called:
                    break
                time.sleep(0.01)
        audited.assert_called_once_with("routine-recovery", result="error", team_id="team_1", detail="deadline-stop")


class ContinuationDeadlineTests(BalanceCase):
    def test_a_deadline_between_continuation_steps_holds_the_partial_run_and_pauses_as_exhausted(self) -> None:
        fired: list[object] = []

        real_resume = routine_recovery.routine_compiled.CompiledRuntime.resume

        def resume(runtime, context, results):
            turn = real_resume(runtime, context, results)
            if runtime.cursor.segment and runtime.cursor.step == 2:
                # The retried step completed; the deadline passes before the next step is dispatched.
                fired[-1]()
            return turn

        brain = Brain("retry")
        assistant = Assistant([failed(), RECORD], [{"outcome": "not_occurred"}])
        controller_plan = (
            ("zones", "list-zones", LOOKUP_INPUT),
            ("create", "create-record", {"zone_id": ZONE, "name": "www"}),
            ("check", "list-zones", LOOKUP_INPUT),
        )
        with (
            tempfile.TemporaryDirectory() as directory,
            mock.patch.object(routine_recovery.threading, "Timer", captured_timer(fired)),
            mock.patch.object(routine_recovery.routine_compiled.CompiledRuntime, "resume", resume),
            mock.patch.object(self, "plan", lambda service, *_steps: type(self).plan(service, *controller_plan)),
        ):
            service, value, run_id = self.run_held(directory, assistant, brain)
            state = self.state(service)
            cursor = self.cursor(service, run_id)
        # Never ended as stopped: the partial run is held with its evidence and its Routine runs no further cycle.
        self.assertEqual(self.status, "held")
        self.assertEqual([item.incident_id for item in state.incidents], [run_id])
        self.assertEqual(cursor.step, 2)
        self.assertEqual((state.notices[-1].outcome, state.notices[-1].detail["reason"]), ("paused", "exhausted"))
        self.assertTrue(record.routine(state, value.routine_id).paused)
        self.assertEqual([action for action, _id in assistant.calls].count("create-record"), 2)

    def test_the_timer_is_set_for_what_is_left_and_nothing_starts_once_it_ran_out(self) -> None:
        delays: list[float] = []

        brain = Brain("retry")
        assistant = Assistant([failed()], [{"outcome": "not_occurred"}])
        with tempfile.TemporaryDirectory() as directory:
            service, _value, run_id = self.held_with_balance(directory, 30, assistant, brain)
            run = mock.Mock(team_id="team_1", run_id=run_id, provider="openai", token=run_id)
            # Taken at 0, armed at 12, and already past its deadline when the episode would start work.
            ticks = iter([0.0, 12.0])
            with (
                mock.patch.object(routine_recovery.threading, "Timer", captured_timer([], delays)),
                mock.patch.object(routine_recovery, "_clock", side_effect=lambda: next(ticks, 30.0)),
            ):
                outcome = routine_recovery.automatic(service, run, API_KEY)
            state = self.state(service)
        self.assertEqual(delays, [18.0])
        self.assertEqual((outcome, brain.asked), ("held", []))
        self.assertNotIn("find-record", [action for action, _id in assistant.calls])
        self.assertEqual((state.notices[-1].outcome, state.notices[-1].detail["reason"]), ("paused", "exhausted"))

    def test_a_retry_with_no_time_left_is_never_dispatched(self) -> None:
        brain = Brain("retry")
        assistant = Assistant([failed(), RECORD], [{"outcome": "not_occurred"}])
        with tempfile.TemporaryDirectory() as directory:
            service, _value, run_id = self.held_with_balance(directory, 30, assistant, brain)
            run = mock.Mock(team_id="team_1", run_id=run_id, provider="openai", token=run_id)
            # In time through the decision, with less than a second left for the retry.
            ticks = iter([0.0, 0.0, 0.0, 0.0, 29.5])
            with (
                service._exclusive_chat_turn("team_1") as token,
                mock.patch.object(routine_recovery, "_clock", side_effect=lambda: next(ticks, 29.5)),
            ):
                run.token = token
                outcome = routine_recovery.automatic(service, run, API_KEY)
            state = self.state(service)
        self.assertEqual((outcome, len(brain.asked)), ("held", 1))
        self.assertEqual([action for action, _id in assistant.calls].count("create-record"), 1)
        self.assertEqual((state.notices[-1].outcome, state.notices[-1].detail["reason"]), ("paused", "exhausted"))

    def test_a_low_balance_retry_continues_with_the_time_its_reservation_has_left(self) -> None:
        brain = Brain("retry")
        assistant = Assistant([failed(), RECORD], [{"outcome": "not_occurred"}])
        balances: list[tuple[int, int]] = []
        real_continue = routine_recovery.continue_run

        def continuing(service, team_id, incident_id, token, progress=None, *, seconds=None):
            held = routine_hold.incident(service.routine_store.load(team_id), incident_id)
            cursor = routine_recovery.routine_incident.open_recovery(service, team_id, incident_id).cursor
            balances.append((held.active_seconds_left, cursor.remaining("recovery_seconds")))
            return real_continue(service, team_id, incident_id, token, progress, seconds=seconds)

        with tempfile.TemporaryDirectory() as directory:
            # All 5 seconds the run had left were reserved; the retry still runs inside them.
            service, _value, run_id = self.held_with_balance(directory, 5, assistant, brain)
            with (
                service._exclusive_chat_turn("team_1") as token,
                mock.patch.object(routine_recovery, "continue_run", side_effect=continuing),
            ):
                run = mock.Mock(team_id="team_1", run_id=run_id, provider="openai", token=token)
                outcome = routine_recovery.automatic(service, run, API_KEY)
            state = self.state(service)
        self.assertEqual(outcome, "recovered")
        self.assertEqual([action for action, _id in assistant.calls].count("create-record"), 2)
        # The run started its continuation with what the reservation had left, never more than its 5 seconds, and
        # the recovery budget got none of it back.
        ((handed, recovery),) = balances
        self.assertTrue(0 < handed <= 5)
        self.assertEqual(recovery, 55)
        self.assertEqual((state.incidents, state.runs, state.notices[-1].outcome), ((), (), "recovered"))

    def test_a_deadline_just_before_the_continuation_registers_still_holds_and_pauses(self) -> None:
        fired: list[object] = []

        real_provider = routine_recovery._provider
        calls: list[str] = []

        def provider(service, team_id):
            calls.append(team_id)
            if len(calls) == 2:
                # The verifier asked first; now the run was reopened, and the deadline passes before its continuation
                # registers.
                self.assertEqual(service.routine_store.load(team_id).incidents, ())
                fired[-1]()
            return real_provider(service, team_id)

        brain = Brain("retry")
        assistant = Assistant([failed(), RECORD], [{"outcome": "not_occurred"}])
        with (
            tempfile.TemporaryDirectory() as directory,
            mock.patch.object(routine_recovery.threading, "Timer", captured_timer(fired)),
            mock.patch.object(routine_recovery, "_provider", side_effect=provider),
        ):
            service, value, run_id = self.run_held(directory, assistant, brain)
            state = self.state(service)
        # Out of time, not stopped: the partial run is held again and its Routine pauses as exhausted.
        self.assertEqual(self.status, "held")
        self.assertEqual([item.incident_id for item in state.incidents], [run_id])
        self.assertEqual((state.notices[-1].outcome, state.notices[-1].detail["reason"]), ("paused", "exhausted"))
        self.assertTrue(record.routine(state, value.routine_id).paused)

    def test_a_deadline_that_cancels_the_continuation_before_it_reopens_still_pauses_as_exhausted(self) -> None:
        fired: list[object] = []

        real_continued = routine_recovery.routine_cursor.continued

        def continued(cursor):
            # The deadline passes while the continuation is being prepared, before the run reopens.
            fired[-1]()
            return real_continued(cursor)

        brain = Brain("retry")
        assistant = Assistant([failed(), RECORD], [{"outcome": "not_occurred"}])
        with (
            tempfile.TemporaryDirectory() as directory,
            mock.patch.object(routine_recovery.threading, "Timer", captured_timer(fired)),
            mock.patch.object(routine_recovery.routine_cursor, "continued", side_effect=continued),
        ):
            service, value, run_id = self.run_held(directory, assistant, brain)
            state = self.state(service)
        # The refused continuation is no plain hold: the incident and its evidence stay, and the Routine pauses.
        self.assertEqual(self.status, "held")
        self.assertEqual([item.incident_id for item in state.incidents], [run_id])
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
            run = mock.Mock(team_id="team_1", run_id=run_id, provider="openai", token=run_id)
            self.assertEqual(routine_recovery.automatic(service, run, API_KEY), "held")
        self.assertNotIn("find-record", [action for action, _id in assistant.calls])

    def cut_after_the_last_step(self, cause: str, exhaust: str = ""):
        """Run a retried continuation and cancel it, by its deadline or a person, once every step is sealed complete."""
        fired: list[object] = []
        box: list[object] = []

        real_resume = routine_recovery.routine_compiled.CompiledRuntime.resume

        def resume(runtime, context, results):
            turn = real_resume(runtime, context, results)
            if runtime.cursor.segment and runtime.cursor.step == 2:
                if exhaust:
                    # The deadline also spent the run's active time, or let its lease lapse.
                    changes = {"time": {"active_seconds_left": 0}, "lease": {"lease_expires_at": 1}}[exhaust]
                    run_id = runtime.cursor.binding.run_id
                    box[0].routine_store.update(
                        "team_1",
                        lambda state: (
                            record._replace_run(state, dataclasses.replace(record.run(state, run_id), **changes)),
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
            service.routine_store.update(
                "team_1",
                lambda state: (
                    record._replace_routine(
                        state, dataclasses.replace(record.routine(state, value.routine_id), failures=2)
                    ),
                    None,
                ),
            )
            return value

        assistant = Assistant([failed(), RECORD], [{"outcome": "not_occurred"}])
        with (
            tempfile.TemporaryDirectory() as directory,
            mock.patch.object(self, "service", service),
            mock.patch.object(self, "routine", failing_streak),
            mock.patch.object(routine_recovery.threading, "Timer", captured_timer(fired)),
            mock.patch.object(routine_recovery.routine_compiled.CompiledRuntime, "resume", resume),
        ):
            service_value, value, _run_id = self.run_held(directory, assistant, Brain("retry"))
            state = self.state(service_value)
        return state, record.routine(state, value.routine_id)

    def test_a_deadline_after_the_last_step_records_completion_from_the_sealed_cursor(self) -> None:
        state, routine_value = self.cut_after_the_last_step("deadline")
        self.assertEqual((self.status, state.incidents), ("recovered", ()))
        # Recorded complete from the sealed cursor, with its Actions, and its failure streak reset.
        self.assertEqual(state.notices[-1].outcome, "recovered")
        self.assertEqual(state.notices[-1].detail["plan"]["actions"][-1][:2], ["shimpz-cloudflare", "create-record"])
        self.assertEqual((routine_value.failures, routine_value.paused), (0, False))

    def test_a_persons_stop_after_the_last_step_still_stops_the_run(self) -> None:
        state, routine_value = self.cut_after_the_last_step("person")
        self.assertEqual((self.status, state.incidents, state.notices[-1].outcome), ("stopped", (), "stopped"))
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
            run = routine_recovery.routine_run._Run(
                "team_1", run_id, record.Lease("0" * 64, "k"), "token", "openai", value
            )
            with self.assertRaises(routine_recovery.ApiProblem) as caught:
                routine_recovery.routine_run.complete_sealed(service, run, None)
        self.assertEqual(caught.exception.code, "routine-lease-invalid")
